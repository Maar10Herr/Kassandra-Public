# Security policy

Kassandra is experimental research software for trusted analysts. No
independent security audit has been performed; production risk-engine use is
outside its scope.

## Reporting

Use GitHub's private vulnerability-reporting or Security Advisory interface.
Do not attach confidential company data, credentials, source documents, or
database files to public issues. Include the affected release, deployment
boundary, reproduction steps, and the smallest safe proof of concept.

## Supported versions

Only the latest tagged release receives security fixes.

## Deployment boundary

The dashboard binds to loopback, requires a per-process bearer session, and
sets restrictive browser headers. Keep that boundary intact. Run collection
behind an outbound firewall when source domains are not fully trusted: URL and
redirect validation plus standard-transport address pinning materially reduce
SSRF and DNS-rebinding risk. Custom transports and other egress paths still
require network controls. Never expose the SQLite database, evidence store, or
dashboard directly to the public internet.
