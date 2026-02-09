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
        script_name = ""

        # Log for debugging (skip health checks to reduce noise)
        if path_info != "/health":
            print(f"[HA-Ingress] Request: PATH_INFO={path_info}, X-Ingress-Path={ingress_path}", file=sys.stderr)

        if ingress_path:
            # Set SCRIPT_NAME so Flask generates correct URLs
            script_name = ingress_path.rstrip("/")
            environ["SCRIPT_NAME"] = script_name
            if path_info != "/health":
                print(f"[HA-Ingress] Set SCRIPT_NAME={script_name}", file=sys.stderr)

        # Wrap start_response to fix redirect URLs
        def patched_start_response(status, headers, exc_info=None):
            # If this is a redirect and we have an ingress path, fix the Location header
            if ingress_path and status.startswith(("301", "302", "303", "307", "308")):
                new_headers = []
                for name, value in headers:
                    if name.lower() == "location":
                        # If Location is absolute path without ingress prefix, add it
                        if value.startswith("/") and not value.startswith(script_name):
                            value = script_name + value
                            if path_info != "/health":
                                print(f"[HA-Ingress] Fixed redirect to: {value}", file=sys.stderr)
                        elif path_info != "/health":
                            print(f"[HA-Ingress] Redirect to: {value}", file=sys.stderr)
                    new_headers.append((name, value))
                headers = new_headers
            elif path_info != "/health":
                print(f"[HA-Ingress] Response: {status}", file=sys.stderr)

            return start_response(status, headers, exc_info)

        return self.app(environ, patched_start_response)


# Create the wrapped application
_app = create_app()
application = HAIngressMiddleware(_app)
