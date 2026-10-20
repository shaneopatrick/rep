#!/usr/bin/env python3
"""Retired with the move to briefs; kept as a no-op.

An install from before the move still runs this file on every edit. If the
file disappeared before `python3 install.py` re-registered the hooks, Python
would exit 2 ("can't open file"), which Claude Code reads as "block", and
every edit in every repo would be refused. Remove after everyone reinstalls.
"""
