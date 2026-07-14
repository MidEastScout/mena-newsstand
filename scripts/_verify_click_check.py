#!/usr/bin/env python3
"""One-shot check for .github/workflows/verify-click-worker.yml: confirms the
test click posted by that workflow shows up in /clicks. Safe to delete along
with that workflow once the Worker redeploy is confirmed."""
import json

with open("/tmp/clicks.json", encoding="utf-8") as f:
    data = json.load(f)

hit = [c for c in data.get("clicks", [])
       if c.get("url") == "https://example.com/__verify-click-worker-test"]
print("Test click counted:", bool(hit), hit)
assert hit, "test click was not counted — Worker may not be redeployed yet"
print("CLICK TRACKING WORKS")
