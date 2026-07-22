from __future__ import annotations

import time

import requests

import temporary_foundersfund_extraction as base


def fast_get(session: requests.Session, url: str, *, timeout: int = 60) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, 5):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                response.raise_for_status()
            if response.status_code >= 500 or response.status_code in {408, 429}:
                raise RuntimeError(f"transient HTTP {response.status_code}")
            response.raise_for_status()
            return response
        except requests.HTTPError:
            raise
        except Exception as exc:
            last = exc
            if attempt == 4:
                break
            time.sleep(min(8, 2**attempt))
    raise RuntimeError(f"GET failed for {url}: {last}")


base.get = fast_get

if __name__ == "__main__":
    base.main()
