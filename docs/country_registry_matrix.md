# Country Registry Access Matrix

Relevant EU countries for corporate early-warning, prioritised by portfolio exposure.

Release snapshot: August 2026. Access modes are observations from development,
not promises of availability or legal permission. Re-check official terms,
network access, and automation rules before operating an adapter.

| Country | Code | Official Registry | API/Access Mode | Free? | Notes |
|---------|------|-------------------|-----------------|-------|-------|
| **France** | FR | [BODACC](https://www.bodacc.fr) / [Infogreffe](https://www.infogreffe.fr) | Open-data JSON API (BODACC) + paid Infogreffe | ✅ Free (BODACC) / 💰 Paid (Infogreffe) | BODACC implemented in Kassandra. Monitors Procédures collectives + Radiations. |
| **Netherlands** | NL | [KVK Handelsregister](https://www.kvk.nl/english/) | [KVK API](https://developers.kvk.nl/) — requires developer registration + API key | 📝 Signup-only | Free after KVK developer account approval. REST API with company search and basic profile data. Rate-limited. Not confirmed free for bulk/automated polling. |
| **Spain** | ES | [Registro Mercantil](https://www.registradores.org/) / [BOE](https://www.boe.es) | BOE [BORME RSS feed](https://www.boe.es/rss/borme.php) (official, free) | ✅ Free (RSS) / 💰 Paid (Registro Mercantil) | **Implemented in Kassandra.** BOE publishes insolvency announcements (concurso de acreedores) via official RSS. RSS feed tested working June 2026. |
| **Italy** | IT | [Registro Imprese](https://www.registroimprese.it) / [Portale dei Creditori](https://www.portalecreditori.it) | No free API. Web scraping possible. | 💰 Paid | Chamber-of-commerce based. Portal Creditori has insolvency data but requires login. Open data limited to basic company info via [ dati.camera.it](https://dati.camera.it). |
| **Belgium** | BE | [Banque-Carrefour des Entreprises (BCE)](https://kbopub.economie.fgov.be/kbopub/) / [Belgisch Staatsblad](https://www.ejustice.just.fgov.be/tsv/) | KBO public search HTML-only. Staatsblad search returns 500 errors. | ✅ Free (manual) / ❌ No API | KBO public search is free but HTML-only. No structured API found (tested June 2026). CSV dumps available for periodic download but not real-time. Not automatable without scraping. |
| **Ireland** | IE | [Companies Registration Office (CRO)](https://www.cro.ie) / [Irish Gazette (Iris Oifigiúil)](https://www.irisoifigiuil.ie) | CRO web-only (no API). Gazette HTML search — no RSS feed found (tested June 2026). | 📝 Signup (CRO) / ❌ No RSS | CRO requires paid account for document access. Irish Gazette publishes liquidations/examinerships but has no structured feed. Not automatable without scraping. |
| **Finland** | FI | [Finnish Patent and Registration Office (PRH)](https://www.prh.fi/en/kaupparekisteri.html) / [Virre](https://virre.prh.fi) | PRH open data REST API; reachability was network-dependent in June 2026 | ⚠️ Variable | API availability must be checked from the deployment environment. Insolvency (konkurssi) notices via Virre may require per-document fees. |

## Access Mode Legend

- ✅ **Free**: No registration or payment required. Ready to integrate.
- 📝 **Signup-only**: Free data after developer account registration. Requires API key management.
- 💰 **Paid**: Requires per-query payment or subscription. Not viable for automated monitoring.
- ⚠️ **Blocked**: Geoblocked or network-unreachable from current deployment location.
- ❌ **No API**: No structured feed or API available; HTML-only access requiring scraping.
