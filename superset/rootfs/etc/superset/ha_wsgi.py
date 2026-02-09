"""
WSGI wrapper for Home Assistant ingress support.

This module wraps the Superset Flask app with middleware that handles
the X-Ingress-Path header set by Home Assistant's ingress proxy.
"""

import re
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
        is_html = False
        response_headers = []

        # Log for debugging (skip health checks to reduce noise)
        if path_info != "/health":
            print(f"[HA-Ingress] Request: PATH_INFO={path_info}, X-Ingress-Path={ingress_path}", file=sys.stderr)

        if ingress_path:
            # Set SCRIPT_NAME so Flask generates correct URLs
            script_name = ingress_path.rstrip("/")
            environ["SCRIPT_NAME"] = script_name
            if path_info != "/health":
                print(f"[HA-Ingress] Set SCRIPT_NAME={script_name}", file=sys.stderr)

        # Wrap start_response to capture headers and fix redirects
        def capturing_start_response(status, headers, exc_info=None):
            nonlocal is_html, response_headers

            new_headers = []
            for name, value in headers:
                # Check if this is HTML content
                if name.lower() == "content-type" and "text/html" in value.lower():
                    is_html = True

                # Fix redirect URLs
                if ingress_path and name.lower() == "location":
                    if value.startswith("/") and not value.startswith(script_name):
                        value = script_name + value
                        if path_info != "/health":
                            print(f"[HA-Ingress] Fixed redirect to: {value}", file=sys.stderr)

                # Skip Content-Length for HTML (we'll recalculate after rewriting)
                if is_html and ingress_path and name.lower() == "content-length":
                    continue

                new_headers.append((name, value))

            response_headers = new_headers

            if path_info != "/health":
                print(f"[HA-Ingress] Response: {status}, HTML={is_html}", file=sys.stderr)

            # If HTML and ingress, defer start_response until we rewrite the body
            if is_html and ingress_path:
                return lambda s: None  # Dummy write function

            return start_response(status, new_headers, exc_info)

        # Get the response
        response = self.app(environ, capturing_start_response)

        # If HTML and we have an ingress path, rewrite the body
        if is_html and ingress_path:
            # Collect response body
            body_parts = []
            for chunk in response:
                body_parts.append(chunk)
            if hasattr(response, 'close'):
                response.close()

            body = b"".join(body_parts)

            try:
                # Decode, rewrite URLs, re-encode
                text = body.decode("utf-8")

                # Rewrite absolute URLs for static assets and API
                # Match href="/...", src="/...", url("/...")
                text = re.sub(
                    r'(href|src|action)="(/(?:static|api|superset|login|logout|dashboard|chart|explore|sqllab|tablemodelview|databaseview|savedqueryview)[^"]*)"',
                    rf'\1="{script_name}\2"',
                    text
                )
                # Also fix url() in inline styles
                text = re.sub(
                    r'url\("(/static[^"]+)"\)',
                    rf'url("{script_name}\1")',
                    text
                )
                # Fix absolute URLs in JavaScript
                text = text.replace('"/static/', f'"{script_name}/static/')
                text = text.replace("'/static/", f"'{script_name}/static/")

                body = text.encode("utf-8")

                if path_info != "/health":
                    print(f"[HA-Ingress] Rewrote HTML body ({len(body)} bytes)", file=sys.stderr)
            except Exception as e:
                print(f"[HA-Ingress] Error rewriting HTML: {e}", file=sys.stderr)

            # Add Content-Length header
            response_headers.append(("Content-Length", str(len(body))))

            # Now call the real start_response
            start_response("200 OK", response_headers)

            return [body]

        return response


# Create the wrapped application
_app = create_app()
application = HAIngressMiddleware(_app)
