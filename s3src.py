"""Amazon S3 (and S3-compatible: Cloudflare R2, Backblaze B2, MinIO, Wasabi)
folders - photos are read IN PLACE, never copied to this PC.

A folder is written as  s3://bucket/optional/prefix .  The access key needs
only read rights (s3:ListBucket and s3:GetObject). No extra Python package
is used: requests are signed here (AWS Signature Version 4).
"""
import datetime
import hashlib
import hmac
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import config
import gdrive
import sourcecfg

EMPTY_SHA = hashlib.sha256(b"").hexdigest()
NS = re.compile(r"^\{.*?\}")


class S3Error(gdrive.DriveError):
    pass


# ---------------------------------------------------------------- settings
def settings():
    d = sourcecfg.load().get("s3") or {}
    return {
        "access_key": d.get("access_key", ""), "secret_key": d.get("secret_key", ""),
        "region": d.get("region") or "ap-south-1", "endpoint": (d.get("endpoint") or "").rstrip("/"),
    }


def connected():
    s = settings()
    return bool(s["access_key"] and s["secret_key"])


def parse_link(text):
    """'s3://bucket/prefix' -> 's3://bucket/prefix/' (or 's3://bucket/'), else None."""
    text = (text or "").strip()
    m = re.fullmatch(r"s3://([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])(?:/(.*))?", text, re.I)
    if not m:
        return None
    prefix = (m.group(2) or "").strip("/")
    return f"s3://{m.group(1)}/" + (prefix + "/" if prefix else "")


def split(entry):
    m = re.fullmatch(r"s3://([^/]+)/(.*)", entry)
    return (m.group(1), m.group(2)) if m else (None, None)


def label(entry):
    return "S3: " + entry[5:].rstrip("/")


# ----------------------------------------------------------------- signing
def _hmac(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _q(s, safe="-_.~"):
    return urllib.parse.quote(s, safe=safe)


def sign(method, host, path, query, headers, access_key, secret_key, region,
         now=None, service="s3"):
    """AWS Signature V4 -> the headers to send. `headers` are extra headers to sign
    (lower-case names). Returns a dict of headers including Authorization."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    h = {"host": host, "x-amz-content-sha256": EMPTY_SHA, "x-amz-date": amz}
    h.update({k.lower(): v.strip() for k, v in headers.items()})
    names = sorted(h)
    canon_q = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(query.items()))
    canon = "\n".join([
        method, _q(path, "/-_.~"), canon_q,
        "".join(f"{n}:{h[n]}\n" for n in names), ";".join(names), EMPTY_SHA])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope,
                         hashlib.sha256(canon.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), day), region), service),
              "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = dict(h)
    out.pop("host")
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                            f"SignedHeaders={';'.join(names)}, Signature={sig}")
    return out, sig


def _target(bucket, key):
    """(scheme+host, path) - path style for custom endpoints, virtual-host style on AWS."""
    s = settings()
    if s["endpoint"]:
        u = urllib.parse.urlparse(s["endpoint"])
        return u.scheme, u.netloc, f"/{bucket}/{key}"
    if "." in bucket:                              # dots break the TLS name of virtual hosting
        return "https", f"s3.{s['region']}.amazonaws.com", f"/{bucket}/{key}"
    return "https", f"{bucket}.s3.{s['region']}.amazonaws.com", f"/{key}"


def _open(bucket, key="", query=None, extra=None, tries=5):
    s = settings()
    if not connected():
        raise S3Error("S3 is not connected yet. Enter the access key on the admin Settings page.")
    scheme, host, path = _target(bucket, key)
    query = query or {}
    for attempt in range(tries):
        hdrs, _ = sign("GET", host, path, query, extra or {}, s["access_key"], s["secret_key"],
                       s["region"])
        url = f"{scheme}://{host}{_q(path, '/-_.~')}"
        if query:
            url += "?" + "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(query.items()))
        req = urllib.request.Request(url, headers=hdrs)
        try:
            return urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise S3Error(_explain(e.code, body, bucket))
        except (urllib.error.URLError, OSError) as e:
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise S3Error(f"Could not reach S3 ({e}).")


def _explain(code, body, bucket):
    if "NoSuchBucket" in body:
        return f"The bucket '{bucket}' does not exist."
    if "InvalidAccessKeyId" in body or "SignatureDoesNotMatch" in body:
        return "S3 did not accept the access key. Check the key, secret and region on the Settings page."
    if "PermanentRedirect" in body or "AuthorizationHeaderMalformed" in body:
        return "The bucket is in another region. Set that region on the Settings page."
    if code in (401, 403):
        return "No permission. The key needs s3:ListBucket and s3:GetObject on this bucket."
    if code == 404:
        return "That file or bucket was not found."
    return f"S3 error {code}."


# ------------------------------------------------------------------ listing
def stamp(info):
    return f"{info.get('size')}|{info.get('modified')}|{info.get('etag', '')}"


def walk(entry):
    """Every photo below s3://bucket/prefix/ -> (relative path, info). Only the
    list is read here."""
    bucket, prefix = split(entry)
    token = None
    while True:
        q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            q["continuation-token"] = token
        with _open(bucket, "", q) as r:
            root = ET.fromstring(r.read())
        for c in root:
            if NS.sub("", c.tag) != "Contents":
                continue
            f = {NS.sub("", x.tag): (x.text or "") for x in c}
            key = f.get("Key", "")
            rel = key[len(prefix):]
            if not rel or key.endswith("/") or Path(rel).suffix.lower() not in config.IMAGE_EXT:
                continue
            yield rel, {"id": f"s3:{bucket}/{key}", "name": Path(rel).name,
                        "size": f.get("Size"), "modified": f.get("LastModified"),
                        "etag": f.get("ETag", "").strip('"'), "src": "s3"}
        more = next((x.text for x in root if NS.sub("", x.tag) == "IsTruncated"), "false")
        nxt = next((x.text for x in root if NS.sub("", x.tag) == "NextContinuationToken"), None)
        if more != "true" or not nxt:
            return
        token = nxt


def check(entry):
    """Cheap test that the bucket and key work: lists one object."""
    bucket, prefix = split(entry)
    with _open(bucket, "", {"list-type": "2", "prefix": prefix, "max-keys": "1"}) as r:
        r.read()


def _ref(ref):
    m = re.fullmatch(r"s3:([^/]+)/(.*)", ref)
    if not m:
        raise S3Error("Not an S3 photo.")
    return m.group(1), m.group(2)


def open_original(ref):
    bucket, key = _ref(ref)
    return _open(bucket, key)


def fetch_full(ref):
    with open_original(ref) as r:
        data = r.read(config.MAX_PHOTO_BYTES + 1)
    if len(data) > config.MAX_PHOTO_BYTES:
        raise S3Error("This file is too large to read.")
    return data
