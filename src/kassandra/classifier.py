"""Content-based event classification.

Extracts material events from web page content and feed entries using
keyword and pattern matching. No LLM calls — transparent, auditable rules.

Per kassandra_goals.md §12 event taxonomy.
Per engineering audit: must classify FULL text, not just excerpts.
Must store matched spans, pattern IDs, and classifier version.

P0 #2: Multilingual patterns (DE/FR/NL/IT/ES), instrumentation,
hard negative controls, yield report.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Bump classifier version for multilingual patterns + instrumentation
CLASSIFIER_VERSION = "2.1.0"

# ── Language markers for detection ────────────────────────────────────────────
# Simple word-frequency based language detection for financial/corporate texts.
# Each language has common function words + domain-specific keywords.
LANG_MARKERS: dict[str, list[str]] = {
    "de": [
        " und ", " die ", " der ", " das ", " ist ", " nicht ", " von ",
        " mit ", " sich ", " auf ", " für ", " werden ", " ein ", " eine ",
        " aber ", " oder ", " auch ", " kann ", " nach ", " bei ", " über ",
        "insolvenz", "zahlungsunfähig", "umstrukturierung", "gewinnwarnung",
        "massenentlassung", "restrukturierungsplan", "fortführungsprognose",
    ],
    "fr": [
        " et ", " les ", " des ", " est ", " pas ", " une ", " dans ",
        " que ", " qui ", " par ", " sur ", " pour ", " avec ", " plus ",
        " mais ", " dont ", " entre ", " elle ", " son ", " ses ",
        " de ", " du ", " au ", " en ", " ce ",
        "insolvabilité", "faillite", "restructuration", "licenciement",
        "avertissement", "sauvegarde", "redressement", "dépôt",
    ],
    "nl": [
        " en ", " van ", " de ", " het ", " een ", " niet ", " dat ",
        " zijn ", " wordt ", " maar ", " ook ", " nog ", " heeft ",
        " kunnen ", " bij ", " door ", " deze ", " voor ", " met ",
        "insolventie", "faillissement", "herstructurering", "winstwaarschuwing",
        "ontslagen", "surseance", "banenverlies",
    ],
    "it": [
        " e ", " che ", " non ", " una ", " sono ", " per ", " con ",
        " come ", " dei ", " della ", " degli ", " nel ", " nella ",
        " più ", " anche ", " stato ", " hanno ", " tra ",
        "insolvenza", "fallimento", "ristrutturazione", "licenziamenti",
        "esuberi", "liquidazione", "concordato",
    ],
    "es": [
        " y ", " que ", " los ", " las ", " del ", " por ", " una ",
        " para ", " con ", " más ", " pero ", " entre ", " como ",
        " está ", " han ", " sido ", " esta ", " sus ",
        "insolvencia", "quiebra", "reestructuración", "despidos",
        "concurso", "acreedores", "expediente",
    ],
}


def detect_language(text: str) -> str:
    """Detect language of corporate/financial text via word-frequency scoring.

    Returns ISO 639-1 language code ('en', 'de', 'fr', 'nl', 'it', 'es').
    Defaults to 'en' when no language has strong signal.
    """
    text_lower = text.lower()
    scores: dict[str, int] = {}

    for lang, markers in LANG_MARKERS.items():
        count = 0
        for marker in markers:
            count += text_lower.count(marker)
        scores[lang] = count

    if not scores:
        return "en"

    best_lang = max(scores, key=scores.get)  # type: ignore[arg-type]
    best_score = scores[best_lang]

    # Require at least 3 markers to be confident it's non-English;
    # otherwise default to English (most texts in this system are English)
    if best_score >= 3:
        return best_lang
    return "en"


# ── Multilingual event classification patterns ────────────────────────────────
# 3-5 patterns per language for common deterioration signals.
# Pattern IDs: P019-P0xx for non-English, prefixed with language code.
# These are applied IN ADDITION to English patterns when language is detected.

MULTILINGUAL_PATTERNS: dict[str, list[tuple[str, str, str, list[str]]]] = {
    # German — Insolvenz, Umstrukturierung, Gewinnwarnung, Entlassungen, Going Concern
    "de": [
        ("insolvency", "critical", "P019-de", [
            r"\binsolven(?:z|t)\b", r"\bzahlungsunf(?:ä|ae)hig(?:keit)?\b",
            r"\binsolvenzantrag\b", r"\binsolvenzverfahren\b",
            r"\b(?:eröffnet|beantragt|gestellt).{0,20}insolvenz\b",
            r"\bgl(?:ä|ae)ubiger(?:schutz|versammlung)\b",
        ]),
        ("restructuring", "high", "P020-de", [
            r"\b(?:restrukturierung|restrukturierungsplan|restrukturierungsprogramm)\b",
            r"\bumstrukturierung\b",
            r"\b(?:sanierung|sanierungsplan|sanierungsverfahren)\b",
            r"\b(?:massenentlassung|stellenabbau|personalabbau)\b",
        ]),
        ("profit_warning", "medium", "P021-de", [
            r"\bgewinnwarnung\b", r"\bergebniswarnung\b",
            r"\b(?:prognose|ausblick)\s+(?:gesenkt|reduziert|korrigiert)\b",
            r"\bumsatzwarnung\b",
        ]),
        ("going_concern_warning", "high", "P022-de", [
            r"\bfort(?:führungs|bestehens)(?:prognose|risiko)\b",
            r"\bbestandsgef(?:ä|ae)hrdung\b",
            r"\b(?:wesentliche|erhebliche)\s+unsicherheit\b",
        ]),
        ("layoffs", "medium", "P023-de", [
            r"\b(?:stellenabbau|personalabbau|massenentlassung|entlassungen?)\b",
            r"\b(?:kurzarbeit|kurzarbeitergeld)\b",
        ]),
    ],
    # French — insolvabilité, restructuration, avertissement, licenciements
    "fr": [
        ("insolvency", "critical", "P019-fr", [
            r"\binsolvabilit(?:é|e)\b", r"\bfaillite\b",
            r"\bd(?:é|e)p(?:ô|o)t\s+de\s+bilan\b",
            r"\bd(?:é|e)pos(?:é|e)\s+son\s+bilan\b",
            r"\b(?:redressement|liquidation)\s+judiciaire\b",
            r"\bcessation\s+de\s+paiements?\b",
            r"\bproc(?:é|e)dure\s+(?:de\s+)?sauvegarde\b",
        ]),
        ("restructuring", "high", "P020-fr", [
            r"\brestructuration\b", r"\bplan\s+de\s+(?:sauvegarde|restructuration)\b",
            r"\b(?:suppression|r(?:é|e)duction)\s+d['’]emplois?\b",
            r"\bplan\s+social\b", r"\bplan\s+de\s+d(?:é|e)parts?\b",
        ]),
        ("profit_warning", "medium", "P021-fr", [
            r"\bavertissement\s+(?:sur|de)\s+(?:r(?:é|e)sultats|b(?:é|e)n(?:é|e)fices?)\b",
            r"\b(?:perspectives|pr(?:é|e)visions|objectifs)\s+(?:abaiss|r(?:é|e)vis|revu)\b",
            r"\bprofit\s*warning\b",
        ]),
        ("going_concern_warning", "high", "P022-fr", [
            r"\bcontinuit(?:é|e)\s+d['’]exploitation\b",
            r"\bincertitude\s+(?:significative|importante|mat(?:é|e)rielle)\b",
        ]),
        ("layoffs", "medium", "P023-fr", [
            r"\blicenciements?\b", r"\bplan\s+social\b",
            r"\bch(?:ô|o)mage\s+(?:partiel|technique)\b",
        ]),
    ],
    # Dutch — insolventie, herstructurering, winstwaarschuwing, ontslagen
    "nl": [
        ("insolvency", "critical", "P019-nl", [
            r"\binsolvent(?:ie|e|verklaard)\b", r"\bfaillissement\b",
            r"\b(?:failliet|faillietverklaring)\b",
            r"\bsurseance\s+van\s+betaling\b",
            r"\buitstel\s+van\s+betaling\b",
        ]),
        ("restructuring", "high", "P020-nl", [
            r"\bherstructurer(?:ing|en)\b", r"\breorganis(?:atie|eren)\b",
            r"\b(?:massaontslag|banenverlies|personeelsreductie)\b",
            r"\bsaneringsplan\b",
        ]),
        ("profit_warning", "medium", "P021-nl", [
            r"\bwinstwaarschuwing\b", r"\bresultaatwaarschuwing\b",
            r"\b(?:prognose|verwachting|outlook)\s+(?:verlaagd|bijgesteld|neerwaarts)\b",
        ]),
        ("going_concern_warning", "high", "P022-nl", [
            r"\bcontinu(?:ï|i)teit(?:svraagstuk|sprobleem|srisico)\b",
            r"\b(?:materi(?:ë|e)le|aanzienlijke)\s+onzekerheid\b",
            r"\bgoing\s*concern\b.*\b(?:onzeker|probleem|risico)\b",
        ]),
        ("layoffs", "medium", "P023-nl", [
            r"\bontslag(?:en|ronde)?\b", r"\bmassaontslag\b",
            r"\b(?:banen|personeel)\s+(?:schrappen|verdwijnen|verlies)\b",
        ]),
    ],
    # Italian — insolvenza, ristrutturazione, profit warning, licenziamenti
    "it": [
        ("insolvency", "critical", "P019-it", [
            r"\binsolvenza\b", r"\bfallimento\b", r"\bliquidazione\b",
            r"\b(?:concordato|amministrazione)\s+(?:preventivo|straordinaria|controllata)\b",
            r"\bstato\s+di\s+(?:insolvenza|crisi)\b",
        ]),
        ("restructuring", "high", "P020-it", [
            r"\bristrutturazion(?:e|i)\b", r"\bpiano\s+di\s+(?:risanamento|ristrutturazione)\b",
            r"\b(?:esuberi|tagli)\s+(?:di|del)\s+personal[eo]\b",
            r"\bcassa\s+integrazione\b",
        ]),
        ("profit_warning", "medium", "P021-it", [
            r"\bprofit\s*warning\b", r"\ballerta\s+(?:utili|risultati)\b",
            r"\b(?:previsioni|stime|guidance)\s+(?:ridott|rivist|tagliat)\b",
        ]),
        ("going_concern_warning", "high", "P022-it", [
            r"\bcontinuit(?:à|a)\s+aziendale\b",
            r"\b(?:significativa|rilevante|sostanziale)\s+incertezza\b",
        ]),
        ("layoffs", "medium", "P023-it", [
            r"\blicenziament[io]\b", r"\besuber[io]\b",
            r"\b(?:tagli|riduzioni?)\s+(?:al|del)\s+personal[eo]\b",
        ]),
    ],
    # Spanish — insolvencia, reestructuración, profit warning, despidos
    "es": [
        ("insolvency", "critical", "P019-es", [
            r"\binsolvencia\b", r"\bquiebra\b",
            r"\bconcurso\s+(?:de|voluntario|necesario)\s+(?:de\s+)?acreedores\b",
            r"\bdeclaraci(?:ó|o)n\s+de\s+(?:insolvencia|concurso|quiebra)\b",
            r"\bpreconcurso\b",
        ]),
        ("restructuring", "high", "P020-es", [
            r"\breestructuraci(?:ó|o)n\b",
            r"\b(?:plan|programa)\s+de\s+reestructuraci(?:ó|o)n\b",
            r"\b(?:despidos?|expediente)\s+(?:colectivo|de\s+regulaci(?:ó|o)n)\b",
            r"\b(?:ere|erte)\b",
        ]),
        ("profit_warning", "medium", "P021-es", [
            r"\badvertencia\s+(?:de|sobre)\s+(?:beneficios|resultados)\b",
            r"\bprofit\s*warning\b",
            r"\b(?:previsiones|estimaciones|gu(?:í|i)as?)\s+(?:reduc|revis|recort)\b",
        ]),
        ("going_concern_warning", "high", "P022-es", [
            r"\bempresa\s+en\s+funcionamiento\b",
            r"\b(?:duda|incertidumbre)\s+(?:sustancial|significativa|material)\b.{0,30}\bcontinuidad\b",
            r"\bincertidumbre\s+(?:significativa|material)\b",
        ]),
        ("layoffs", "medium", "P023-es", [
            r"\bdespidos?\b", r"\bexpediente\s+(?:de\s+regulaci(?:ó|o)n|colectivo)\b",
            r"\b(?:ere|erte)\b",
        ]),
    ],
}

# Event classification patterns: (event_type, severity, pattern_id, patterns)
# Patterns are case-insensitive regex applied to text content
# Negative patterns — if matched, discard the event
NEGATIVE_PATTERNS: dict[str, list[str]] = {
    "restructuring": [
        r"\bfinance\s+transformation\b",  # IT project, not distressed restructuring
        r"\bdigital\s+transformation\b",
        r"\bsolution\s+emerging\b",
        r"\bnot\s+a\s+cost[\s-]*cutting\b",  # explicitly denied
        r"\b(?:voluntary|early\s+retirement)\s+(?:leave|program)\b",  # voluntary, not distressed
        r"\b(?:reposition|reshape|moderni[sz]e)\b.*\b(?:workforce|company)\b",  # strategic, not distressed
        # Denial patterns
        r"\bno\s+restructuring\s+(?:program|plan)\s+(?:is|being)\b",
        r"\b(?:categorically|explicitly|firmly|formally)\s+denied\b",
        r"\b(?:not|no)\s+(?:a|part\s+of\s+a)\s+cost[\s-]*cutting\b",
    ],
    "insolvency": [
        r"\binsolvency\s+outlook\b",  # macro report, not entity-specific distress
        r"\bglobal\s+insolvency\s+outlook\b",
        r"\bbrace\s+for\b.*\binsolvency\b",
        r"\bliquidation\s+(?:sale|price|discount|clearance)\b",  # retail, not corporate
        # Denial patterns — explicit statements that insolvency is NOT happening
        r"\bnot\s+(?:entering|in)\s+(?:insolven|bankrupt|administration)\b",
        r"\b(?:no|not)\s+(?:intention|plan|plans)\s+to\s+(?:enter|file)\b.*\binsolven",
        r"\b(?:not|never)\s+(?:been|gone)\s+into\s+(?:insolven|administration)\b",
    ],
    "cyber_incident": [
        r"\brisk\s+of\b.*\b(?:hacked|hacking)\b",  # Risk advisory, not incident
        r"\b(?:travel|abroad|geopolitical)\b.*\b(?:hack|cyber)",
        r"\b(?:survey|opinionway|cesin)\b",  # Survey, not incident
        r"\bone\s+in\s+(?:two|three|four|five)\b",  # Statistical claim
        # Hypothetical / risk assessment language — not an actual incident
        r"\b(?:hypothetical|purely\s+analytical|what.if|risk\s+assessment|scenario\s+analysis)\b",
        r"\bhas\s+not\s+experienced\s+(?:any|a)\s+(?:material\s+)?cyber",
    ],
    "litigation_material": [
        r"\bclimate.related\s+litigation\b",  # Risk disclosure
        r"\brisk\s+factor",  # Boilerplate
    ],
    "profit_warning": [
        r"\bchallenging\s+yourself\b",
        r"\bchallenging\s+the\s+feedback\b",
        r"\bnegative\s+impact\s+on\s+(?:mental\s+)?health\b",
        r"\bscreens\s+have\s+a\s+negative\s+impact\b",
    ],
    "payment_stress": [
        r"\bdiversification\s+of\s+funding\s+sources\b",
        r"\bminimi[sz]e\s+refinancing\s+risk\b",
        r"\brefinancing\s+risk\s+management\b",
    ],
}

EVENT_PATTERNS: list[tuple[str, str, str, list[str]]] = [
    ("insolvency", "critical", "P001", [
        r"\binsolven(?:t|cy)\b", r"\bbankruptcy\b", r"\bchapter\s*11\b",
        r"\bwinding[-\s]*up\b", r"\bliquidation\b", r"\badministration\s+order\b",
        r"\bcreditors[-\s]*(?:meeting|protection)\b", r"\breceivership\b",
        r"\bdissolution\b", r"\bstrike[-\s]*off\b",
        r"\benter(?:ed|s)\s+(?:administration|compulsory\s+liquidation)\b",
        r"\bfiled\s+for\s+(?:insolvency|bankruptcy|administration|chapter\s*11)\b",
        r"\bno\s+choice\s+but\s+to\s+(?:enter|take\s+steps\s+to\s+enter)\b",
    ]),
    ("restructuring", "high", "P002", [
        r"\brestructuring\s+(?:plan|program|charge)\b", r"\breorganization\b",
        r"\bcapital\s+reduction\b", r"\bdebt\s+restructuring\b",
        r"\bcost[-\s]*cutting\s+program\b", r"\btransformation\s+program\b",
        r"\bseverance\s+(?:program|plan)\b", r"\bvoluntary\s+arrangement\b",
        r"\bdeleveraging\s+plan\b", r"\bdebt[-\s]*for[-\s]*equity\b",
        r"\b(?:lenders|creditors)\s+(?:take|taking)\s+control\b",
        r"\b(?:reject|rejected)\s+(?:restructur|deleverag|rescue)\b",
    ]),
    ("going_concern_warning", "high", "P003", [
        r"\bgoing\s*concern\b", r"\bmaterial\s*uncertainty\b",
        r"\bsubstantial\s*doubt.*continue\b",
        r"\bability\s*to\s*continue.*going\s*concern\b",
    ]),

    # High severity
    ("profit_warning", "medium", "P004", [
        r"\bprofit\s*warning\b", r"\bearnings\s*warning\b",
        r"\blower\s*(?:than|ed).*(?:guidance|outlook|forecast|expectation)\b",
        r"\brevenue\s*warning\b", r"\bdownward\s*revision\b",
        r"\bguidance\s*(?:cut|reduc|lower)\b",
        r"\b(?:cautious|weaker|deteriorat(?:ing|ed)?)\s+(?:trading|market|demand)\s+(?:outlook|conditions?)\b",
        r"\b(?:dividend|payout)\s+(?:suspend|cut|cancelled|scrapped)\b",
        r"\bwrite[-\s]down\b.*\b(?:million|billion)\b",
        r"\b(?:provision|charge)s?\b.*\b(?:million|billion)\b",
        r"\bnet\s+debt\s+higher\s+than\b",
        r"\b(?:further|additional)\s+(?:contract|provision|write[-\s]?down)s?\b",
    ]),
    ("payment_stress", "high", "P005", [
        r"\bcovenant\s*(?:breach|waiver|amendment)\b", r"\bdefault\s+notice\b",
        r"\bpayment\s+default\b", r"\bdebt\s+default\b",
        r"\bcredit\s+rating\s+(?:downgrade|negative\s+watch)\b",
        r"\b(?:elevated|material|acute|near[-\s]?term)\s+refinancing\s+risk\b",
        r"\brefinancing\s+risk\b.*\b(?:covenant|maturity|liquidity|default|downgrade|waiver)\b",
        r"\bliquidity\s+(?:crisis|concern|shortfall)\b",
        r"\bcash\s+(?:position|balance)\s+(?:is|of)\s+(?:only\s+)?£?[0-9.,]+\s*(?:m|million|k|thousand)",
        r"\b(?:actual|real)\s+cash\b",
    ]),
    ("refinancing_stress", "high", "P005b", [
        r"\b(?:bailout|rescue)\s+(?:talks|package|deal|loan|facility)\b",
        r"\bcontingency\s+facility\b", r"\bemergency\s+(?:funding|loan|facility)\b",
        r"\b(?:banks|lenders)\s+(?:demand|require).*\b(?:additional|extra|further)\b",
        r"\b(?:rescue|bailout)\s+(?:talks|negotiations)\s+(?:collapse|fail|break)\b",
        r"\brecapitalis(?:ation|e)\b",
    ]),
    ("layoffs", "medium", "P006", [
        r"\blay[-\s]+\s*off\b", r"\bredundan(?:t|cy|cies)\b", r"\bjob\s*cut\b",
        r"\bworkforce\s*reduction\b", r"\bheadcount\s*reduction\b",
        r"\bstaff\s*reduction\b", r"\bdownsiz\b", r"\bshort[-\s]time\s*work\b",
    ]),
    # P006b: exclude sports "injury layoff" context
    # (handled by pattern specificity — injury layoff won't match corporate patterns)

    # Medium severity
    ("management_departure", "low", "P007", [
        r"\b(?:ceo|cfo|chief\s+\w+\s+officer|chairman|president)\s+(?:resign|step\s*down|depart|leav|exit)\b",
        r"\bexecutive\s+(?:resign|departure|exit)\b",
    ]),
    ("auditor_departure", "medium", "P008", [
        r"\bauditor\s+(?:resign|removal|departure|dismiss)\b",
        r"\bresignation\s+of\s+(?:the\s+)?auditor\b",
        r"\b(?:external|independent)\s+auditor\s+(?:resign|step\s*down|refuses\s+to\s+sign)\b",
        r"\b(?:refuses|refused)\s+to\s+sign\s+off\b",
    ]),
    ("auditor_warning", "high", "P008b", [
        r"\b(?:qualified|adverse)\s+audit\s+(?:report|opinion)\b",
        r"\bunable\s+to\s+verify\b",
        r"\bcannot\s+(?:be\s+)?verif(?:y|ied)\b",
        r"\bspecial\s+audit\b.*\b(?:find|found|report)\b",
        r"\bauditor\s+(?:warn|flag|raise|identif)\b",
    ]),
    ("regulatory_action", "high", "P009", [
        r"\bregulatory\s+(?:action|fine|penalty|investigation|enforcement)\b",
        r"\bantitrust\s+(?:investigation|fine)\b", r"\bcompetition\s+authority\b",
        r"\bdoj\s+(?:investigation|subpoena)\b", r"\bsec\s+(?:investigation|fine)\b",
        r"\bdata\s+protection\s+(?:fine|breach|investigation)\b",
    ]),
    ("cyber_incident", "high", "P010", [
        r"\bcyber\s*(?:attack|breach)\b", r"\bramsomware\b",
        r"\bdata\s*breach\b", r"\bhack(?:ed|ing)\b",
        r"\bunauthorized\s+access\b",
        r"\b(?:suffered|experienced|reported|disclosed|detected|confirmed)\s+.*\bcyber",
        r"\bsecurity\s+(?:incident|breach)\s+(?:at|on|in)\b",
    ]),
    # P010b: exclude surveys ("one in two companies reported") from real incidents
    ("litigation_material", "medium", "P011", [
        r"\bclass\s*action\b", r"\blawsuit\b", r"\blitigation\b",
        r"\blegal\s+proceeding\b", r"\bcourt\s+(?:ruling|order)\b",
        r"\bsettlement\s+(?:payment|agreement)\b", r"\bdamages\s+award\b",
        r"\b(?:investigation|probe)\s+(?:alleges|finds|reveals|discovers)\s+(?:fraud|fraudulent|misconduct|irregularit)",
        r"\b(?:arrested|charged)\s+(?:on|with)\s+(?:fraud|bribery|corruption|money\s+laundering)",
        r"\b(?:ceo|executive|director|officer)\b.*\b(?:arrested|detained|charged)\b",
        r"\bfraudulent\s+(?:accounting|financial|trading)\b",
        r"\baccounting\s+irregularit",
    ]),

    # Lower severity
    ("hiring_freeze", "low", "P012", [
        r"\bhiring\s*freeze\b", r"\brecruitment\s*freeze\b",
        r"\bsuspension\s*of\s*hiring\b",
    ]),
    ("facility_closure", "high", "P013", [
        r"\b(?:plant|facility|factory|site|office)\s+(?:closure|closing|shutdown|shut\s*down)\b",
        r"\bproduction\s+(?:halt|interruption|stoppage)\b",
    ]),
    ("contract_loss", "medium", "P014", [
        r"\bcontract\s+(?:loss|lost|termination|cancellation)\b",
        r"\bcustomer\s+(?:loss|lost|termination)\b",
        r"\b(?:lost|lose)\s+(?:major|key|significant)\s+(?:customer|contract|client)\b",
    ]),
    ("recall", "medium", "P015", [
        r"\bproduct\s*recall\b", r"\bsafety\s*recall\b",
        r"\bvoluntary\s*recall\b", r"\bwithdrawal\s+of\s+product\b",
    ]),
    ("environmental_incident", "medium", "P016", [
        r"\boil\s*spill\b", r"\bchemical\s*(?:spill|leak)\b",
        r"\benvironmental\s+(?:incident|disaster|damage|breach)\b",
        r"\bpollution\s+(?:incident|event)\b",
    ]),

    # Positive/unconfirmed
    ("contract_win", "low", "P017", [
        r"\bcontract\s+(?:win|award|signing)\b",
        r"\bnew\s+(?:major|key)\s+customer\b",
    ]),
    ("unconfirmed_adverse", "low", "P018", [
        r"\breportedly\s+(?:faces|facing|headed|heading|set\s+to|going\s+to|planning|considering)",
        r"\b(?:rumour|speculation)\b.*\b(?:insolven|bankrupt|restructur|layoff|default)\b",
        r"\b(?:sources|people\s+familiar)\s+(?:say|claim|report|indicate)\b",
    ]),
]


# ── Instrumentation ────────────────────────────────────────────────────────────

@dataclass
class ClassifierRun:
    """Tracks classification statistics across a batch of documents.

    Records: documents processed/with-text/skipped, candidate hits,
    rejected candidates, accepted events, and breakdowns by source and language.
    """

    documents_processed: int = 0
    documents_with_text: int = 0
    documents_skipped: int = 0
    candidate_pattern_hits: int = 0
    rejected_candidates: int = 0
    accepted_events: int = 0
    by_source: dict[str, dict[str, int]] = field(default_factory=dict)
    by_language: dict[str, dict[str, int]] = field(default_factory=dict)

    def record_document(
        self,
        *,
        has_text: bool,
        skipped: bool = False,
        source: str = "unknown",
        language: str = "en",
        candidates: int = 0,
        rejected: int = 0,
        accepted: int = 0,
    ) -> None:
        """Record one document's classification results."""
        self.documents_processed += 1
        if skipped:
            self.documents_skipped += 1
            return
        if has_text:
            self.documents_with_text += 1
        self.candidate_pattern_hits += candidates
        self.rejected_candidates += rejected
        self.accepted_events += accepted

        # Per-source breakdown
        if source not in self.by_source:
            self.by_source[source] = {"docs": 0, "candidates": 0, "rejected": 0, "accepted": 0}
        self.by_source[source]["docs"] += 1
        self.by_source[source]["candidates"] += candidates
        self.by_source[source]["rejected"] += rejected
        self.by_source[source]["accepted"] += accepted

        # Per-language breakdown
        if language not in self.by_language:
            self.by_language[language] = {"docs": 0, "candidates": 0, "rejected": 0, "accepted": 0}
        self.by_language[language]["docs"] += 1
        self.by_language[language]["candidates"] += candidates
        self.by_language[language]["rejected"] += rejected
        self.by_language[language]["accepted"] += accepted

    def build_yield_report(self) -> dict[str, Any]:
        """Build a classifier yield report dict.

        Returns:
            {
                total_docs, docs_with_text, docs_skipped,
                candidates, accepted, rejected,
                by_source: {source: {docs, candidates, rejected, accepted}},
                by_language: {language: {docs, candidates, rejected, accepted}},
                accepted_rate (0-1 or None),
            }
        """
        total = self.documents_processed
        accepted_rate = (
            round(self.accepted_events / max(self.candidate_pattern_hits, 1), 3)
            if self.candidate_pattern_hits > 0
            else None
        )
        return {
            "total_docs": total,
            "docs_with_text": self.documents_with_text,
            "docs_skipped": self.documents_skipped,
            "candidates": self.candidate_pattern_hits,
            "accepted": self.accepted_events,
            "rejected": self.rejected_candidates,
            "accepted_rate": accepted_rate,
            "by_source": dict(self.by_source),
            "by_language": dict(self.by_language),
        }


# ── Core classification ───────────────────────────────────────────────────────

def _make_event(
    event_type: str,
    severity: str,
    pattern_id: str,
    pattern: str,
    match: re.Match[str],
    text: str,
) -> dict[str, Any]:
    """Build an event dict from a regex match."""
    match_len = match.end() - match.start()
    confidence = min(0.5 + (match_len / 20.0), 0.95)

    ctx_start = max(0, match.start() - 60)
    ctx_end = min(len(text), match.end() + 60)
    matched_text = text[ctx_start:ctx_end].replace("\n", " ").strip()

    return {
        "event_type": event_type,
        "severity": severity,
        "pattern_id": pattern_id,
        "matched_pattern": pattern,
        "matched_text": matched_text[:500],
        "span_start": match.start(),
        "span_end": match.end(),
        "confidence": round(confidence, 2),
        "classifier_version": CLASSIFIER_VERSION,
    }


def _classify_with_patterns(
    text: str,
    text_lower: str,
    patterns: list[tuple[str, str, str, list[str]]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Run pattern matching against text. Returns (events, candidates, rejected)."""
    events: list[dict[str, Any]] = []
    candidates = 0
    rejected = 0

    for event_type, severity, pattern_id, pat_list in patterns:
        for pat in pat_list:
            match = re.search(pat, text_lower)
            if match:
                candidates += 1
                # Check negative patterns
                neg_patterns = NEGATIVE_PATTERNS.get(event_type, [])
                neg_match = False
                for neg_pat in neg_patterns:
                    if re.search(neg_pat, text_lower):
                        neg_match = True
                        break
                if neg_match:
                    rejected += 1
                    break

                events.append(_make_event(event_type, severity, pattern_id, pat, match, text))
                break  # One match per event_type per pattern group

    return events, candidates, rejected


def _chunk_text(text: str, chunk_size: int = 3000, overlap: int = 500) -> list[tuple[str, int]]:
    """Split text into overlapping chunks for classification.

    P1 (engineering audit 2026-06-20): Long documents can bury adverse signals deep
    in the text. Chunking ensures signals anywhere in a document are detected.

    Returns list of (chunk_text, byte_offset) tuples.
    Short texts (< chunk_size) return a single chunk.
    """
    if len(text) <= chunk_size:
        return [(text, 0)]

    chunks: list[tuple[str, int]] = []
    offset = 0

    while offset < len(text):
        chunk = text[offset:offset + chunk_size]
        chunks.append((chunk, offset))
        offset += chunk_size - overlap
        if offset >= len(text):
            break

    return chunks


def classify_content(
    text: str,
    url: str = "",
    *,
    run: ClassifierRun | None = None,
    source: str = "unknown",
) -> list[dict[str, Any]]:
    """Classify text content into material events using pattern matching.

    Strips HTML tags before classification to avoid matching boilerplate.
    Searches FULL text — not limited to excerpts.
    Supports multilingual detection for DE/FR/NL/IT/ES texts.

    When ``run`` is provided, classification statistics are recorded
    for instrumentation and yield reporting.

    Returns list of events: {
        event_type, severity, pattern_id, matched_pattern,
        matched_text, span_start, span_end, confidence
    }
    """
    has_text = bool(text and len(text.strip()) >= 30)
    skipped = not has_text

    if run is not None:
        run.documents_processed += 1
        if skipped:
            run.documents_skipped += 1
            return []

    if not has_text:
        return []

    if run is not None:
        run.documents_with_text += 1

    # Strip HTML tags to avoid classifying boilerplate
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) < 30:
        return []

    text_lower = text.lower()

    # Detect language for multilingual patterns
    lang = detect_language(text_lower)

    total_candidates = 0
    total_rejected = 0
    all_events: list[dict[str, Any]] = []

    # P1 (engineering audit): Chunk long texts so adverse signals deep in documents aren't missed.
    # Previously only the full text as one unit was classified — long documents
    # could have signals beyond where pattern engines effectively scan.
    chunks = _chunk_text(text, chunk_size=3000, overlap=500)
    seen_event_keys: set[tuple[str, int]] = set()  # dedup within document

    for chunk_idx, (chunk_text, chunk_offset) in enumerate(chunks):
        chunk_lower = chunk_text.lower()

        # English patterns (always run)
        eng_events, eng_cand, eng_rej = _classify_with_patterns(
            chunk_text, chunk_lower, EVENT_PATTERNS
        )
        total_candidates += eng_cand
        total_rejected += eng_rej

        # Deduplicate and adjust span offsets
        for ev in eng_events:
            ev_key = (ev["pattern_id"], chunk_offset + ev.get("span_start", 0) // 100 * 100)
            if ev_key not in seen_event_keys:
                seen_event_keys.add(ev_key)
                ev["span_start"] = ev.get("span_start", 0) + chunk_offset
                ev["span_end"] = ev.get("span_end", 0) + chunk_offset
                all_events.append(ev)

        # Multilingual patterns (run in addition to English when language matches)
        if lang != "en" and lang in MULTILINGUAL_PATTERNS:
            ml_events, ml_cand, ml_rej = _classify_with_patterns(
                chunk_text, chunk_lower, MULTILINGUAL_PATTERNS[lang]
            )
            total_candidates += ml_cand
            total_rejected += ml_rej

            for ev in ml_events:
                ev_key = (ev["pattern_id"], chunk_offset + ev.get("span_start", 0) // 100 * 100)
                if ev_key not in seen_event_keys:
                    seen_event_keys.add(ev_key)
                    ev["span_start"] = ev.get("span_start", 0) + chunk_offset
                    ev["span_end"] = ev.get("span_end", 0) + chunk_offset
                    all_events.append(ev)

    if run is not None:
        run.candidate_pattern_hits += total_candidates
        run.rejected_candidates += total_rejected
        run.accepted_events += len(all_events)
        # Per-source
        if source not in run.by_source:
            run.by_source[source] = {"docs": 0, "candidates": 0, "rejected": 0, "accepted": 0}
        run.by_source[source]["docs"] += 1
        run.by_source[source]["candidates"] += total_candidates
        run.by_source[source]["rejected"] += total_rejected
        run.by_source[source]["accepted"] += len(all_events)
        # Per-language
        if lang not in run.by_language:
            run.by_language[lang] = {"docs": 0, "candidates": 0, "rejected": 0, "accepted": 0}
        run.by_language[lang]["docs"] += 1
        run.by_language[lang]["candidates"] += total_candidates
        run.by_language[lang]["rejected"] += total_rejected
        run.by_language[lang]["accepted"] += len(all_events)

    return all_events


def classify_documents(
    documents: list[dict[str, str]],
    *,
    run: ClassifierRun | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Classify a batch of documents with instrumentation.

    Each document dict should have:
        - ``text`` (required): the document text content
        - ``source`` (optional): source identifier
        - ``url`` (optional): document URL

    Args:
        documents: List of document dicts.
        run: Optional existing ClassifierRun to accumulate into.
             If None, a new run is created.

    Returns:
        (all_events, yield_report) — all events from all documents,
        and a yield report dict from the ClassifierRun.
    """
    if run is None:
        run = ClassifierRun()

    all_events: list[dict[str, Any]] = []

    for doc in documents:
        text = doc.get("text", "") or ""
        source = doc.get("source", "unknown")
        url = doc.get("url", "")

        events = classify_content(text, url, run=run, source=source)
        all_events.extend(events)

    return all_events, run.build_yield_report()


def extract_events_from_evidence(
    db: Any,
    evidence_id: int,
    registry_id: int,
    use_full_text: bool = True,
    skip_html: bool = False,
) -> int:
    """Classify evidence content and store material events.

    When use_full_text=True: loads full evidence content from disk.
    When skip_html=True: skips text/html content entirely (legacy behavior).
    Default (False): extracts visible text from HTML via BeautifulSoup
    and classifies it — homepages/IR pages may contain adverse announcements
    (restructuring press releases, profit warnings, etc.) not published via RSS.
    Negative patterns filter common corporate-marketing false positives.

    Returns count of new events created.
    """
    from kassandra.evidence import store_event, get_evidence_path

    row = db.execute(
        "SELECT content_hash, excerpt, source_url, content_type, content_length "
        "FROM evidence WHERE id = ?",
        (evidence_id,),
    ).fetchone()

    if not row:
        return 0

    is_html = row["content_type"] and "html" in row["content_type"].lower()

    # Legacy skip — skip entirely if explicitly requested
    if skip_html and is_html:
        return 0

    text_to_classify = None

    if use_full_text:
        # Try loading full content from disk
        evidence_path = get_evidence_path(db, evidence_id)
        if evidence_path and evidence_path.exists():
            try:
                content_bytes = evidence_path.read_bytes()
                raw_text = content_bytes.decode("utf-8", errors="replace")

                # P2-3: Extract visible text from HTML instead of skipping
                if is_html:
                    try:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(raw_text, "html.parser")
                        # Remove script/style/nav/footer/header — boilerplate, not adverse content
                        for tag in soup(["script", "style", "nav", "footer", "header"]):
                            tag.decompose()
                        text_to_classify = soup.get_text(separator="\n", strip=True)
                        logger.debug(
                            f"Extracted {len(text_to_classify)} chars of visible text "
                            f"from HTML ({len(raw_text)} raw) for evidence {evidence_id}"
                        )
                    except Exception as e:
                        logger.debug(f"BeautifulSoup extraction failed: {e}, using raw text")
                        text_to_classify = raw_text
                else:
                    text_to_classify = raw_text

                logger.debug(
                    f"Classifying full text ({len(text_to_classify)} chars) "
                    f"for evidence {evidence_id}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to load full evidence content: {e}, "
                    f"falling back to excerpt"
                )

    if not text_to_classify:
        text_to_classify = row["excerpt"] or ""
        logger.debug(
            f"Classifying excerpt ({len(text_to_classify)} chars) "
            f"for evidence {evidence_id}"
        )

    events = classify_content(text_to_classify, row["source_url"])
    created = 0

    for event in events:
        ev_result = store_event(
            db=db,
            evidence_id=evidence_id,
            registry_id=registry_id,
            event_type=event["event_type"],
            severity=event["severity"],
            confidence=event["confidence"],
            description=event.get("matched_text", "")[:500],
            source_claims_directly=True,
            raw_event_json=json.dumps(event, ensure_ascii=False, default=str),
        )
        if ev_result.status == "inserted":
            created += 1

    return created


# ── Database-backed classifier run recording ──────────────────────────────────

def record_classifier_run(
    db: Any,
    run_id: str,
    source_name: str,
    language: str | None,
    stats: dict[str, int],
) -> int:
    """Record a classifier run row for per-source yield tracking.

    Args:
        db: Database connection.
        run_id: Collection run identifier.
        source_name: Source identifier (e.g. 'web_monitor', 'companies_house').
        language: Detected language or None.
        stats: Dict with keys: documents_discovered, documents_fetched,
               documents_with_text, documents_classified, candidate_pattern_hits,
               rejected_candidates, accepted_events.

    Returns the row id of the inserted classifier_runs row.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """INSERT INTO classifier_runs
           (run_id, source_name, language,
            documents_discovered, documents_fetched, documents_with_text,
            documents_classified, candidate_pattern_hits, rejected_candidates,
            accepted_events, false_positive_checks, classifier_version,
            created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            source_name,
            language,
            stats.get("documents_discovered", 0),
            stats.get("documents_fetched", 0),
            stats.get("documents_with_text", 0),
            stats.get("documents_classified", 0),
            stats.get("candidate_pattern_hits", 0),
            stats.get("rejected_candidates", 0),
            stats.get("accepted_events", 0),
            stats.get("false_positive_checks", 0),
            CLASSIFIER_VERSION,
            now,
        ),
    )
    db.commit()
    logger.debug(
        f"Recorded classifier run for {source_name}: "
        f"{stats.get('documents_discovered', 0)} discovered, "
        f"{stats.get('accepted_events', 0)} events"
    )
    return cursor.lastrowid


def get_classifier_yield_report(db: Any) -> dict[str, Any]:
    """Get per-source classifier yield summary for digest reporting.

    Returns a dict keyed by source_name with per-source totals and
    yield rates, plus an overall summary.
    """
    # Report the latest run per source. Summing lifetime rows made remediated
    # pre-identity-gate false positives look like current accepted events.
    rows = db.execute("""
        SELECT cr.source_name,
               cr.documents_discovered as total_docs,
               cr.documents_fetched as total_fetched,
               cr.documents_with_text as total_with_text,
               cr.documents_classified as total_classified,
               cr.candidate_pattern_hits as total_candidates,
               cr.rejected_candidates as total_rejected,
               cr.accepted_events as total_events,
               cr.false_positive_checks as total_fp_checks,
               (SELECT COUNT(*) FROM classifier_runs all_cr
                WHERE all_cr.source_name = cr.source_name) as runs
        FROM classifier_runs cr
        JOIN (
            SELECT source_name, MAX(id) AS latest_id
            FROM classifier_runs GROUP BY source_name
        ) latest ON latest.latest_id = cr.id
        ORDER BY cr.documents_discovered DESC
    """).fetchall()

    per_source: dict[str, dict[str, Any]] = {}
    grand_totals = {
        "documents_discovered": 0,
        "documents_fetched": 0,
        "documents_with_text": 0,
        "documents_classified": 0,
        "candidate_pattern_hits": 0,
        "rejected_candidates": 0,
        "accepted_events": 0,
        "false_positive_checks": 0,
        "runs": 0,
    }

    for row in rows:
        source = row["source_name"]
        total_docs = row["total_docs"] or 0
        total_events = row["total_events"] or 0
        total_classified = row["total_classified"] or 0

        # Yield rate: events per document classified (or per document discovered)
        yield_rate = None
        if total_classified > 0:
            yield_rate = round(total_events / total_classified * 100, 1)

        per_source[source] = {
            "total_docs": total_docs,
            "total_fetched": row["total_fetched"] or 0,
            "total_with_text": row["total_with_text"] or 0,
            "total_classified": total_classified,
            "total_candidates": row["total_candidates"] or 0,
            "total_rejected": row["total_rejected"] or 0,
            "total_events": total_events,
            "total_fp_checks": row["total_fp_checks"] or 0,
            "runs": row["runs"] or 0,
            "yield_rate_pct": yield_rate,
        }

        grand_totals["documents_discovered"] += total_docs
        grand_totals["documents_fetched"] += row["total_fetched"] or 0
        grand_totals["documents_with_text"] += row["total_with_text"] or 0
        grand_totals["documents_classified"] += total_classified
        grand_totals["candidate_pattern_hits"] += row["total_candidates"] or 0
        grand_totals["rejected_candidates"] += row["total_rejected"] or 0
        grand_totals["accepted_events"] += total_events
        grand_totals["false_positive_checks"] += row["total_fp_checks"] or 0
        grand_totals["runs"] += row["runs"] or 0

    # Overall yield rate
    overall_yield = None
    if grand_totals["documents_classified"] > 0:
        overall_yield = round(
            grand_totals["accepted_events"]
            / grand_totals["documents_classified"]
            * 100,
            1,
        )

    return {
        "per_source": per_source,
        "totals": grand_totals,
        "overall_yield_rate_pct": overall_yield,
    }
