"""Source probes: is a data source answering from here, right now?

A failed run says something broke on the job node. Probing the same source from another network tells
apart "the source is down for everyone" from "the job node was blocked or got a bad response".
"""
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

CONTACT_USER_AGENT = "QuantConnect-DatasetMonitor/1.0 (+https://www.quantconnect.com; support@quantconnect.com)"
BROWSERLESS_USER_AGENT = "Python-urllib/3"


@dataclass
class ProbeResult:
    dataset_id: int
    url: str
    ok: bool
    status: int = None
    bytes: int = 0
    null_bytes: int = 0
    elapsed_s: float = 0.0
    error: str = None  # network or TLS failure, when there was no HTTP answer
    detail: str = ""  # why ok is False

    def describe(self):
        if self.error:
            return f"{self.url}: {self.error}"
        text = f"{self.url}: HTTP {self.status}, {self.bytes} bytes in {self.elapsed_s:.1f}s"
        if self.null_bytes:
            text += f", {self.null_bytes} null bytes"
        return text + (f" ({self.detail})" if self.detail else "")


def expand_url(template, now=None):
    now = now or datetime.now(timezone.utc)
    return (template.replace("{year}", f"{now.year}").replace("{prev_year}", f"{now.year - 1}")
            .replace("{yyyymm}", now.strftime("%Y%m"))
            .replace("{date_minus_30}", (now - timedelta(days=30)).strftime("%m/%d/%Y")))


def probe(spec, timeout=30, contact_user_agent=CONTACT_USER_AGENT):
    url = expand_url(spec["url"])
    agent = contact_user_agent if spec.get("user_agent") == "contact" else (spec.get("user_agent") or BROWSERLESS_USER_AGENT)
    request = urllib.request.Request(url, headers={"User-Agent": agent, "Accept-Encoding": "identity"})
    expect = int(spec.get("expect_status", 200))
    start = time.monotonic()
    status, body, error = None, b"", None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as http_error:
        status, body = http_error.code, http_error.read() or b""
    except (ssl.SSLError, ssl.CertificateError) as tls_error:
        error = f"TLS error: {tls_error}"
    except urllib.error.URLError as url_error:
        reason = url_error.reason
        error = f"TLS error: {reason}" if isinstance(reason, ssl.SSLError) else f"unreachable: {reason}"
    except (socket.timeout, TimeoutError):
        error = f"timed out after {timeout}s"
    except OSError as os_error:
        error = f"unreachable: {os_error}"
    elapsed = time.monotonic() - start

    result = ProbeResult(spec.get("dataset"), url, False, status, len(body), body.count(b"\x00"), elapsed, error)
    if error:
        return result
    problems = []
    if status != expect:
        problems.append(f"expected HTTP {expect}")
    if result.null_bytes and result.null_bytes > len(body) // 2:
        problems.append("body is mostly null bytes")
    if status == expect and spec.get("min_bytes") and len(body) < int(spec["min_bytes"]):
        problems.append(f"body under {spec['min_bytes']} bytes")
    if status == expect and spec.get("contains") and spec["contains"].encode() not in body:
        problems.append(f"body lacks {spec['contains']!r}")
    result.ok = not problems
    result.detail = "; ".join(problems)
    return result


def probe_url(url, timeout=30):
    """Ad-hoc probe of a URL found in a log: any 2xx/3xx answer counts as reachable."""
    result = probe({"url": url, "user_agent": "contact"}, timeout=timeout)
    if result.status and result.status < 400 and not result.error:
        result.ok, result.detail = True, ""
    return result
