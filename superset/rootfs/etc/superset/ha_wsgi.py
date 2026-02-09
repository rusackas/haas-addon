"""
WSGI wrapper for Home Assistant ingress support.

This module wraps the Superset Flask app with middleware that handles
the X-Ingress-Path header set by Home Assistant's ingress proxy.
"""

import sys
from superset.app import create_app


class HAIngressMiddleware:
    """Middleware to handle Home Assistant ingress path."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        # Get the ingress path from HA's header
        ingress_path = environ.get("HTTP_X_INGRESS_PATH", "")
        path_info = environ.get("PATH_INFO", "/")

        # Log for debugging
        print(f"[HA-Ingress] Request: PATH_INFO={path_info}, X-Ingress-Path={ingress_path}", file=sys.stderr)

        if ingress_path:
            # Set SCRIPT_NAME so Flask generates correct URLs
            script_name = ingress_path.rstrip("/")
            environ["SCRIPT_NAME"] = script_name
            print(f"[HA-Ingress] Set SCRIPT_NAME={script_name}", file=sys.stderr)

        return self.app(environ, start_response)


# Create the wrapped application
_app = create_app()
application = HAIngressMiddleware(_app)
