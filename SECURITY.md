# Security Policy

Uruk isolates many sites inside one deployment, so we take security reports seriously —
tenant isolation, authentication, and HTML escaping above all.

## Supported versions

Uruk is pre-1.0. Security fixes land on `main` and in the next release; older versions do
not receive backports.

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for a vulnerability.**

Report it privately through GitHub's
[private vulnerability reporting](https://github.com/Galaktis/uruk/security/advisories/new)
(the repository's **Security** tab → **Report a vulnerability**). Please include:

- the affected component and version (or commit),
- the steps to reproduce, or a proof of concept,
- the impact you expect (for example, reading another tenant's data).

We aim to acknowledge reports within 3 business days and to keep you informed until a fix
is released. Once it ships, we will publish an advisory and credit you, unless you prefer
to stay anonymous.

## Scope

Examples of what we especially want to hear about:

- reading or changing one tenant's data from another tenant (schema, cache or asset leaks);
- bypassing Django permissions through the REST API, the MCP server or the Studio;
- script injection through editor content outside the explicit `raw_html` element;
- weaknesses in the API tokens, JWTs, or the first-superuser claim flow.

`raw_html` elements render trusted HTML by design; a user who already has permission to
edit them injecting scripts is not a vulnerability.
