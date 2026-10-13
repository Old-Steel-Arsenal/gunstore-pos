# Loaded by the prod image's gunicorn (Dockerfile CMD -c). Trust the scheme the
# frontend nginx relays from Caddy — and ONLY X-Forwarded-Proto: gunicorn's
# default secure_scheme_headers also honour X-Forwarded-Ssl / -Protocol, which
# nothing upstream strips, so a client could 400 a request by contradicting them.
# Any source may set it: the backend publishes no port; only the stack's nginx
# reaches it. Without this the request scheme reads http and Frappe's OAuth
# issuer/endpoints (/.well-known/oauth-authorization-server) say http://.
forwarded_allow_ips = "*"
secure_scheme_headers = {"X-FORWARDED-PROTO": "https"}
