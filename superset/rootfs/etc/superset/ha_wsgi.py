"""
WSGI wrapper for Home Assistant ingress support.

This module wraps the Superset Flask app with middleware that handles
the X-Ingress-Path header set by Home Assistant's ingress proxy.
"""

import gzip
import re
import sys
import zlib

import zstandard as zstd

from superset.app import create_app


class HAIngressMiddleware:
    """Middleware to handle Home Assistant ingress path."""

    def __init__(self, app):
        self.app = app

    def _rewrite_url(self, url, script_name):
        """Rewrite a URL to include the ingress prefix if needed."""
        if url.startswith("/") and not url.startswith(script_name) and not url.startswith("//"):
            return script_name + url
        return url

    def _decompress(self, data, encoding):
        """Decompress data based on Content-Encoding."""
        if encoding == "gzip":
            return gzip.decompress(data)
        elif encoding == "deflate":
            try:
                return zlib.decompress(data)
            except zlib.error:
                # Try raw deflate
                return zlib.decompress(data, -zlib.MAX_WBITS)
        elif encoding == "zstd":
            dctx = zstd.ZstdDecompressor()
            return dctx.decompress(data)
        return data

    def _compress(self, data, encoding):
        """Compress data based on Content-Encoding."""
        if encoding == "gzip":
            return gzip.compress(data)
        elif encoding == "deflate":
            return zlib.compress(data)
        elif encoding == "zstd":
            cctx = zstd.ZstdCompressor()
            return cctx.compress(data)
        return data

    def _rewrite_html(self, text, script_name):
        """Rewrite all absolute URLs in HTML content."""
        # Inject <base> tag right after <head> to handle relative URLs
        # This helps with dynamically loaded resources
        base_tag = f'<base href="{script_name}/">'
        if "<head>" in text and base_tag not in text:
            text = text.replace("<head>", f"<head>{base_tag}", 1)
        elif "<head " in text:
            # Handle <head with attributes
            text = re.sub(
                r"(<head[^>]*>)",
                lambda m: m.group(1) + base_tag,
                text,
                count=1
            )

        # Inject JavaScript to patch fetch() and XMLHttpRequest
        # This is needed because fetch() doesn't respect <base> tags
        fetch_patch = f'''<script>
(function() {{
  var INGRESS_PATH = "{script_name}";

  // Patch fetch to prepend ingress path to absolute URLs
  var originalFetch = window.fetch;
  window.fetch = function(url, options) {{
    if (typeof url === 'string' && url.startsWith('/') && !url.startsWith(INGRESS_PATH) && !url.startsWith('//')) {{
      url = INGRESS_PATH + url;
    }}
    return originalFetch.call(this, url, options);
  }};

  // Patch XMLHttpRequest.open for older AJAX calls
  var originalXHROpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {{
    if (typeof url === 'string' && url.startsWith('/') && !url.startsWith(INGRESS_PATH) && !url.startsWith('//')) {{
      arguments[1] = INGRESS_PATH + url;
    }}
    return originalXHROpen.apply(this, arguments);
  }};

  // Also patch Image src for dynamically created images
  var originalImage = window.Image;
  window.Image = function(width, height) {{
    var img = new originalImage(width, height);
    var originalSrcDescriptor = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
    Object.defineProperty(img, 'src', {{
      set: function(url) {{
        if (typeof url === 'string' && url.startsWith('/') && !url.startsWith(INGRESS_PATH) && !url.startsWith('//')) {{
          url = INGRESS_PATH + url;
        }}
        originalSrcDescriptor.set.call(this, url);
      }},
      get: function() {{
        return originalSrcDescriptor.get.call(this);
      }}
    }});
    return img;
  }};

  console.log('[HA-Ingress] URL patching enabled for path:', INGRESS_PATH);
}})();
</script>'''

        # Insert the patch script right after <head> (after base tag)
        if base_tag in text:
            text = text.replace(base_tag, base_tag + fetch_patch, 1)

        # Rewrite href, src, action with double quotes
        text = re.sub(
            r'(href|src|action|data-src|poster)="(/[^"]*)"',
            lambda m: f'{m.group(1)}="{self._rewrite_url(m.group(2), script_name)}"',
            text
        )

        # Rewrite href, src, action with single quotes
        text = re.sub(
            r"(href|src|action|data-src|poster)='(/[^']*)'",
            lambda m: f"{m.group(1)}='{self._rewrite_url(m.group(2), script_name)}'",
            text
        )

        # Fix url() in CSS (both inline and in style tags)
        text = re.sub(
            r"url\(['\"]?(/[^)'\"]+)['\"]?\)",
            lambda m: f'url("{self._rewrite_url(m.group(1), script_name)}")',
            text
        )

        # Fix srcset attribute (multiple URLs)
        def rewrite_srcset(match):
            srcset = match.group(2)
            parts = []
            for part in srcset.split(","):
                part = part.strip()
                if part.startswith("/") and not part.startswith(script_name):
                    # Split URL from size descriptor
                    tokens = part.split(None, 1)
                    tokens[0] = self._rewrite_url(tokens[0], script_name)
                    parts.append(" ".join(tokens))
                else:
                    parts.append(part)
            return f'{match.group(1)}="{", ".join(parts)}"'

        text = re.sub(
            r'(srcset)="([^"]*)"',
            rewrite_srcset,
            text
        )

        # Fix content attribute for meta refresh redirects
        text = re.sub(
            r'(content="\d+;\s*url=)(/[^"]*)"',
            lambda m: f'{m.group(1)}{self._rewrite_url(m.group(2), script_name)}"',
            text,
            flags=re.IGNORECASE
        )

        # Fix absolute URLs in JavaScript - catch common patterns
        # Pattern: "/static/...", "/api/...", etc.
        js_path_prefixes = (
            "static", "api", "superset", "login", "logout", "dashboard",
            "chart", "explore", "sqllab", "tablemodelview", "databaseview",
            "savedqueryview", "favicon", "assets", "users", "roles",
            "csstemplatemodelview", "annotationlayermodelview", "welcome"
        )
        prefix_pattern = "|".join(js_path_prefixes)

        # Double-quoted JS strings
        text = re.sub(
            rf'"(/(?:{prefix_pattern})(?:/[^"]*)?)"',
            lambda m: f'"{self._rewrite_url(m.group(1), script_name)}"',
            text
        )

        # Single-quoted JS strings
        text = re.sub(
            rf"'(/(?:{prefix_pattern})(?:/[^']*)?)'",
            lambda m: f"'{self._rewrite_url(m.group(1), script_name)}'",
            text
        )

        # Fix JSON in script tags: "url":"/..." or "path":"/..."
        text = re.sub(
            rf'"(url|path|href|src|redirect|next|location)":\s*"(/(?:{prefix_pattern})(?:/[^"]*)?)"',
            lambda m: f'"{m.group(1)}":"{self._rewrite_url(m.group(2), script_name)}"',
            text
        )

        return text

    def __call__(self, environ, start_response):
        # Get the ingress path from HA's header
        ingress_path = environ.get("HTTP_X_INGRESS_PATH", "")
        path_info = environ.get("PATH_INFO", "/")
        script_name = ""
        is_html = False
        is_javascript = False
        content_encoding = None
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
        captured_status = "200 OK"

        def capturing_start_response(status, headers, exc_info=None):
            nonlocal is_html, is_javascript, content_encoding, response_headers, captured_status
            captured_status = status

            new_headers = []
            for name, value in headers:
                # Check content type
                if name.lower() == "content-type":
                    if "text/html" in value.lower():
                        is_html = True
                    elif "javascript" in value.lower():
                        is_javascript = True

                # Check content encoding (compression)
                if name.lower() == "content-encoding":
                    content_encoding = value.lower()

                # Fix redirect URLs
                if ingress_path and name.lower() == "location":
                    if value.startswith("/") and not value.startswith(script_name):
                        value = script_name + value
                        if path_info != "/health":
                            print(f"[HA-Ingress] Fixed redirect to: {value}", file=sys.stderr)

                # Skip Content-Length for rewritable content (we'll recalculate)
                if (is_html or is_javascript) and ingress_path and name.lower() == "content-length":
                    continue

                new_headers.append((name, value))

            response_headers = new_headers

            if path_info != "/health":
                print(f"[HA-Ingress] Response: {status}, HTML={is_html}, JS={is_javascript}, Encoding={content_encoding}", file=sys.stderr)

            # If rewritable content and ingress, defer start_response
            if (is_html or is_javascript) and ingress_path:
                return lambda s: None  # Dummy write function

            return start_response(status, new_headers, exc_info)

        # Get the response
        response = self.app(environ, capturing_start_response)

        # If HTML/JS and we have an ingress path, rewrite the body
        if (is_html or is_javascript) and ingress_path:
            # Collect response body
            body_parts = []
            for chunk in response:
                body_parts.append(chunk)
            if hasattr(response, 'close'):
                response.close()

            body = b"".join(body_parts)

            try:
                # Decompress if needed
                if content_encoding in ("gzip", "deflate", "zstd"):
                    body = self._decompress(body, content_encoding)
                    if path_info != "/health":
                        print(f"[HA-Ingress] Decompressed {content_encoding} ({len(body)} bytes)", file=sys.stderr)

                text = body.decode("utf-8")

                if is_html:
                    text = self._rewrite_html(text, script_name)
                elif is_javascript:
                    # For JavaScript, rewrite common URL patterns
                    js_path_prefixes = (
                        "static", "api", "superset", "login", "logout", "dashboard",
                        "chart", "explore", "sqllab", "assets", "welcome"
                    )
                    prefix_pattern = "|".join(js_path_prefixes)

                    # Rewrite string literals containing absolute paths
                    text = re.sub(
                        rf'"(/(?:{prefix_pattern})(?:/[^"]*)?)"',
                        lambda m: f'"{self._rewrite_url(m.group(1), script_name)}"',
                        text
                    )
                    text = re.sub(
                        rf"'(/(?:{prefix_pattern})(?:/[^']*)?)'",
                        lambda m: f"'{self._rewrite_url(m.group(1), script_name)}'",
                        text
                    )

                body = text.encode("utf-8")

                # Recompress if needed
                if content_encoding in ("gzip", "deflate", "zstd"):
                    body = self._compress(body, content_encoding)
                    if path_info != "/health":
                        print(f"[HA-Ingress] Recompressed {content_encoding} ({len(body)} bytes)", file=sys.stderr)

                if path_info != "/health":
                    content_type = "HTML" if is_html else "JS"
                    print(f"[HA-Ingress] Rewrote {content_type} body", file=sys.stderr)
            except Exception as e:
                print(f"[HA-Ingress] Error rewriting content: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc(file=sys.stderr)

            # Add Content-Length header
            response_headers.append(("Content-Length", str(len(body))))

            # Now call the real start_response with original status
            start_response(captured_status, response_headers)

            return [body]

        return response


# Create the wrapped application
_app = create_app()
application = HAIngressMiddleware(_app)
