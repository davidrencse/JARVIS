"""Owner-built site APIs (Instagram, X, …) behind one small HTTP contract.

Contract: POST {url} with JSON {"query": str, "context": [str]} and an optional
bearer token from the env var named by `token_env`. Response: JSON object with a
string `answer` field (other fields are kept). Only localhost/127.0.0.1 or https
URLs are accepted so tokens never travel in clear text off-machine.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib keeps the bearer token on redirect; never let it follow to another origin.
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def call_api(adapter, query, context=()):
    url = adapter.get("url", "")
    parsed = urllib.parse.urlparse(url)
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError(f"Adapter {adapter.get('name')}: URL must be https or localhost.")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if adapter.get("token_env"):
        token = os.environ.get(adapter["token_env"])
        if not token:
            raise ValueError(f"Set {adapter['token_env']} for adapter {adapter.get('name')}.")
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps({"query": query, "context": list(context)}).encode(),
        headers=headers, method="POST")
    try:
        opener = urllib.request.build_opener(_SameOriginRedirect())
        with opener.open(request, timeout=int(adapter.get("timeout", 120))) as resp:
            data = json.loads(resp.read(5_000_000))
    except urllib.error.HTTPError as exc:
        exc.close()  # the error object holds the response; drop its socket now
        raise RuntimeError(f"Adapter {adapter.get('name')} returned HTTP {exc.code}.") from None
    if not isinstance(data, dict) or not isinstance(data.get("answer"), str):
        raise RuntimeError(f"Adapter {adapter.get('name')} must return JSON with an 'answer'.")
    return data
