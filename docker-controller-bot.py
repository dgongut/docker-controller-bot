#!/usr/bin/env python3
"""
Entry point.

Imports the modules that register commands and inline-button callbacks, then
hands over to the core. Importing is what registers them, so this order is the
guarantee that nothing is missing once polling starts.
"""

import core

if __name__ == "__main__":
	core.main()
