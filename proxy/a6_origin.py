"""Fixed trusted A6 origin; never forward console PAT to redirects.

The legacy a6api.com console began redirecting to a different registrable domain.
The already-used relay origin also serves the console API and is verified directly.
"""
CONSOLE_BASE = 'https://api.a6api.com'
