#!/usr/bin/env python3
import browser_cookie3

jar = browser_cookie3.chrome(domain_name=".buffer.com")
print("; ".join(f"{c.name}={c.value}" for c in jar))