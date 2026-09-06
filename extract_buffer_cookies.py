#!/usr/bin/env python3
"""Dump every Buffer cookie Chrome holds, as a Cookie header string.

Pulling only domain_name=".buffer.com" missed host-only cookies — those set
on publish/auth/login.buffer.com or on bare "buffer.com" — so a session could
be present in the browser and still be absent from what we handed the
container. Collect across the hosts Buffer actually uses and de-duplicate,
keeping the most specific (last-written) value for a name.
"""
import browser_cookie3

HOSTS = ("buffer.com", ".buffer.com", "publish.buffer.com", ".publish.buffer.com",
         "login.buffer.com", "auth.buffer.com", "account.buffer.com",
         ".account.buffer.com", "graph.buffer.com", "callbacks.buffer.com")

jar = {}
for host in HOSTS:
    try:
        for c in browser_cookie3.chrome(domain_name=host):
            if "buffer.com" in (c.domain or ""):
                jar[c.name] = c.value
    except Exception:
        continue

print("; ".join(f"{k}={v}" for k, v in jar.items()))
