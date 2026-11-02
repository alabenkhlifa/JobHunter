"""Explicit HTTPS sharing for one private review workspace."""

import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import re
import time
from urllib.parse import urlsplit


class ReviewAccess:
    cookie_name = "resume_review_session"
    lifetime = 14 * 24 * 60 * 60

    def __init__(self, public_url, token):
        url = urlsplit(public_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or not url.path.endswith("/")
                or not re.fullmatch(r"/(?:[a-zA-Z0-9_-]+/)*", url.path)):
            raise ValueError("Public URL must be an HTTPS workspace URL ending in /.")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{32,128}", token):
            raise ValueError("Use a private token file containing at least 32 URL-safe characters.")
        self.origin = f"https://{url.netloc}"
        self.host = url.netloc
        self.path = url.path
        self.token = token

    def accepts_token(self, value):
        return isinstance(value, str) and hmac.compare_digest(value.encode(), self.token.encode())

    def cookie(self):
        timestamp = str(int(time.time()))
        signature = hmac.new(self.token.encode(), timestamp.encode(), hashlib.sha256).hexdigest()
        return (f"{self.cookie_name}={timestamp}.{signature}; Path={self.path}; "
                f"Max-Age={self.lifetime}; Secure; HttpOnly; SameSite=Strict")

    def authorized(self, headers):
        authorization = headers.get("Authorization", "")
        if authorization.startswith("Bearer ") and self.accepts_token(authorization[7:]):
            return True
        try:
            cookies = SimpleCookie(headers.get("Cookie", ""))
            timestamp, signature = cookies[self.cookie_name].value.split(".", 1)
            age = time.time() - int(timestamp)
            expected = hmac.new(self.token.encode(), timestamp.encode(), hashlib.sha256).hexdigest()
            return 0 <= age <= self.lifetime and hmac.compare_digest(signature, expected)
        except (CookieError, KeyError, ValueError, TypeError):
            return False
