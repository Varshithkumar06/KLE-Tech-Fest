#!/usr/bin/env python3
"""
Backwards-compatibility shim: run_all_agents.py
===============================================
Redirects directly to the unified project runner in run.py.
"""
from run import main

if __name__ == "__main__":
    main()
