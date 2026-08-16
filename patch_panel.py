#!/usr/bin/env python3
"""Patch panel.js to fix evalCommand fallback."""

import os

PATH = "/Users/irpan/Projects/personal/VD/AdobePremiereProMCP/cep-panel/dist/src/panel.js"

with open(PATH, "r") as f:
    content = f.read()

# Old code pattern
old_load = 'if (typeof mcpDispatch !== "function" || typeof \' + fn + \' !== "function") { \' +\n                    \'  try { $.evalFile("\' + premiereJsxPath + \'"); } catch(loadErr) {} \' +\n                    \'} \''

new_load = 'if (typeof mcpDispatch !== "function") { \' +\n                    \'  try { $.evalFile("\' + premiereJsxPath + \'"); } catch(loadErr) {} \' +\n                    \'} \''

old_call = 'var callScript = "mcpDispatch(" + escapeForEval(fn) + "," +\n                escapeForEval(argsJson || "{}") + ")";'

new_call = '''var safeFn = escapeForEval(fn);
            var safeArgs = escapeForEval(argsJson || "{}");
            var callScript =
                'if (typeof mcpDispatch === "function") { ' +
                '  mcpDispatch(' + safeFn + ',' + safeArgs + '); ' +
                '} else if (typeof ' + fn + ' === "function") { ' +
                '  ' + fn + '(' + safeArgs + '); ' +
                '} else { ' +
                '  JSON.stringify({success:false, error:"Function ' + fn + ' not found and premiere.jsx failed to load"}); ' +
                '}';'''

# Apply patches
if old_load in content:
    content = content.replace(old_load, new_load)
    print("✓ Patched loadScript")
else:
    print("✗ loadScript pattern not found")

if old_call in content:
    content = content.replace(old_call, new_call)
    print("✓ Patched callScript")
else:
    print("✗ callScript pattern not found")

# Write back
with open(PATH, "w") as f:
    f.write(content)

print("Done. Panel.js patched.")
