#!/usr/bin/env python3
"""Publish a local MP4 to the public media host and print its public URL.

Usage:  python3 scripts/publish_media.py <local.mp4> [--backend github|r2]

Contract (the Buffer GraphQL sibling depends on this):
  * stdout  -> ONLY the public HTTPS URL, one line, nothing else.
  * stderr  -> all diagnostics.
  * exit 0  -> URL is live and verified (200 / video/mp4 / real MP4 bytes).
  * exit 1  -> publish or verification failed (nothing on stdout).

Why this exists: Buffer's GraphQL API has no binary upload endpoint, so
`createPost` needs `assets: [{video: {url: "https://..."}}]` -- a public,
unauthenticated URL its servers can fetch at publish time.

Backends:
  github (default) -- commit the MP4 into the PUBLIC repo
      https://github.com/knowledgeaidem-netizen/glp1-media  (branch: media)
      served by GitHub Pages at
      https://knowledgeaidem-netizen.github.io/glp1-media/media/<file>.mp4
      Needs: the `gh` CLI authenticated (token scope: repo) and git.
      No Cloudflare credential required.

  r2 -- Cloudflare R2 bucket served over a public r2.dev (or custom) domain.
      Needs: CLOUDFLARE_R2_ACCESS_KEY_ID, CLOUDFLARE_R2_SECRET_ACCESS_KEY,
      CLOUDFLARE_R2_BUCKET, CLOUDFLARE_R2_PUBLIC_URL.
      See scripts/MEDIA_HOSTING.md for how to create that credential.

Idempotent: if the exact bytes are already published, the script prints
"already published" on stderr and re-emits the same URL without re-uploading.
"""
import hashlib
import hmac
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

GITHUB_REPO = "knowledgeaidem-netizen/glp1-media"
GITHUB_BRANCH = "media"
GITHUB_CLONE_URL = "https://github.com/%s.git" % GITHUB_REPO
GITHUB_PAGES_BASE = "https://knowledgeaidem-netizen.github.io/glp1-media"
MEDIA_SUBDIR = "media"

DEFAULT_CLONE_DIR = os.path.expanduser("~/.cache/glp1-media")
CLONE_DIR = os.environ.get("GLP1_MEDIA_CLONE_DIR", DEFAULT_CLONE_DIR)

# Cap the wait: GitHub Pages rebuild is usually < 60s, CDN propagation a bit more.
PUBLISH_TIMEOUT = int(os.environ.get("GLP1_MEDIA_TIMEOUT", "600"))

MP4_FTYP_BRANDS = (b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42",
                   b"avc1", b"dash", b"mmp4", b"M4V ", b"qt  ")


def log(msg):
    """Diagnostics go to stderr; stdout is reserved for the URL."""
    sys.stderr.write("%s\n" % msg)
    sys.stderr.flush()


def run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError("command failed (%d): %s\n%s" % (p.returncode, " ".join(cmd), p.stderr.strip()))
    return p


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_blob_sha(path):
    """Git's blob object id for a file -- lets us compare bytes exactly."""
    size = os.path.getsize(path)
    h = hashlib.sha1()
    h.update(b"blob %d\0" % size)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_mp4(path):
    """True if the file starts with a valid ISO-BMFF ftyp box."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    if len(head) < 12 or head[4:8] != b"ftyp":
        return False
    return any(head[8:12] == b for b in MP4_FTYP_BRANDS)


def http_head(url, timeout=30):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "glp1-publish-media/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {})
    except Exception:
        return 0, {}


def http_range(url, nbytes=16, timeout=60):
    """Fetch the first bytes of a URL (Range request, falls back to a full GET)."""
    req = urllib.request.Request(url, headers={"Range": "bytes=0-%d" % (nbytes - 1),
                                               "User-Agent": "glp1-publish-media/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(nbytes)
    except Exception:
        pass
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "glp1-publish-media/1.0"}),
                                    timeout=timeout) as r:
            return r.read(nbytes)
    except Exception:
        return b""


def verify_public_url(url, expect_size=None, timeout=60):
    """Confirm the URL really serves the MP4 to an anonymous client.

    Checks: 200, content-type video/mp4, byte-length matches, ftyp signature.
    Returns (ok, reason).
    """
    status, headers = http_head(url, timeout=timeout)
    if status != 200:
        return False, "HEAD returned %s" % status
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    if "video/mp4" not in ctype and "application/mp4" not in ctype:
        return False, "unexpected content-type: %r" % ctype
    head = http_range(url, 16, timeout=timeout)
    if len(head) < 12 or head[4:8] != b"ftyp":
        return False, "body is not an MP4 (no ftyp box): %r" % head[:12]
    if head[8:12] not in MP4_FTYP_BRANDS:
        return False, "unexpected ftyp brand: %r" % head[8:12]
    if expect_size is not None:
        clen = headers.get("Content-Length") or headers.get("content-length")
        if clen is not None and clen.isdigit() and int(clen) != expect_size:
            return False, "content-length %s != local %d" % (clen, expect_size)
    return True, "ok"


# --------------------------------------------------------------------------
# GitHub Pages backend
# --------------------------------------------------------------------------

def gh_ensure_clone(clone_dir):
    """Clone or fast-forward the media repo working copy."""
    if not os.path.isdir(os.path.join(clone_dir, ".git")):
        if os.path.isdir(clone_dir) and os.listdir(clone_dir):
            shutil.rmtree(clone_dir)
        os.makedirs(os.path.dirname(clone_dir), exist_ok=True)
        log("cloning %s -> %s" % (GITHUB_CLONE_URL, clone_dir))
        run(["git", "clone", "--branch", GITHUB_BRANCH, GITHUB_CLONE_URL, clone_dir])
    else:
        run(["git", "fetch", "--prune", "origin"], cwd=clone_dir)
    run(["git", "checkout", GITHUB_BRANCH], cwd=clone_dir)
    run(["git", "reset", "--hard", "origin/%s" % GITHUB_BRANCH], cwd=clone_dir)
    return clone_dir


def gh_remote_blob(repo_rel_path, clone_dir):
    """(sha, size) of a file on origin/<branch>, or None if absent."""
    p = run(["git", "ls-tree", GITHUB_BRANCH, "--", repo_rel_path], cwd=clone_dir, check=False)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    parts = p.stdout.split()
    if len(parts) < 4:
        return None
    sha = parts[2]
    size = parts[3]
    return (sha, int(size) if size.isdigit() else None)


def gh_publish(local_path, filename):
    """Commit + push the MP4, then wait for GitHub Pages to serve it."""
    clone_dir = gh_ensure_clone(CLONE_DIR)
    dest_rel = "%s/%s" % (MEDIA_SUBDIR, filename)
    dest_abs = os.path.join(clone_dir, dest_rel)
    os.makedirs(os.path.dirname(dest_abs), exist_ok=True)

    local_sha = git_blob_sha(local_path)
    local_size = os.path.getsize(local_path)

    existing = gh_remote_blob(dest_rel, clone_dir)
    if existing and existing[0] == local_sha:
        log("already published: %s (identical bytes, %d bytes)" % (dest_rel, local_size))
        return "%s/%s" % (GITHUB_PAGES_BASE, dest_rel), True

    shutil.copyfile(local_path, dest_abs)
    run(["git", "add", "--", dest_rel], cwd=clone_dir)
    staged = run(["git", "diff", "--cached", "--name-only"], cwd=clone_dir).stdout.split()
    if not staged:
        log("nothing to commit for %s" % dest_rel)
    else:
        msg = "media: add %s (%d bytes)" % (filename, local_size)
        run(["git", "commit", "-m", msg], cwd=clone_dir)
        log("pushing %s" % msg)
        run(["git", "push", "origin", "%s:%s" % (GITHUB_BRANCH, GITHUB_BRANCH)], cwd=clone_dir)
    return "%s/%s" % (GITHUB_PAGES_BASE, dest_rel), False


# --------------------------------------------------------------------------
# Cloudflare R2 backend (requires a credential the user must create)
# --------------------------------------------------------------------------

def _r2_env():
    need = ["CLOUDFLARE_R2_ACCESS_KEY_ID", "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
            "CLOUDFLARE_R2_BUCKET", "CLOUDFLARE_R2_PUBLIC_URL"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "the r2 backend needs these environment variables: %s\n"
            "See scripts/MEDIA_HOSTING.md -> 'Switching to Cloudflare R2' for how to create them."
            % ", ".join(missing))
    return {k: os.environ[k] for k in need}


UNRESERVED = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
              "-._~")


def _uri_encode(value, safe=""):
    """AWS SigV4 URI encoding: percent-encode everything outside the
    unreserved set, using uppercase hex, and UTF-8 for non-ASCII."""
    out = []
    for byte in value.encode("utf-8"):
        ch = chr(byte)
        if ch in UNRESERVED or ch in safe:
            out.append(ch)
        else:
            out.append("%%%02X" % byte)
    return "".join(out)


def _remove_dot_segments(url):
    """RFC 3986 section 5.2.4 "Remove Dot Segments".

    AWS additionally collapses consecutive slashes, so `//` is dropped too.
    """
    if not url:
        return ""
    output = []
    for seg in url.split("/"):
        if seg and seg != ".":
            if seg == "..":
                if output:
                    output.pop()
            else:
                output.append(seg)
    first = "/" if url[0] == "/" else ""
    last = "/" if (url[-1] == "/" and output) else ""
    return first + "/".join(output) + last


def _canonical_uri(path, s3_style=False):
    """Canonical URI for the request.

    Generic SigV4: normalize the path (remove dot segments, collapse
    duplicate slashes), then URI-encode it.

    S3-style (used by S3 and R2): the path is used VERBATIM -- not
    normalized and not re-encoded. This is what botocore's S3SigV4Auth does
    and what R2's S3-compatible endpoint expects.
    """
    if s3_style:
        return path or "/"
    normalized = _remove_dot_segments(path or "/")
    return _uri_encode(normalized, safe="/~")


def _canonical_query(query):
    """Canonical query string: sort by key then value, URI-encode both."""
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if part == "":
            continue
        if "=" in part:
            k, v = part.split("=", 1)
        else:
            k, v = part, ""
        pairs.append((_uri_encode(k), _uri_encode(v)))
    pairs.sort()
    return "&".join("%s=%s" % (k, v) for k, v in pairs)


def _canonical_headers(headers):
    """Canonical headers: lowercase names, trimmed/compressed values, sorted.

    `headers` is an ordered list of (name, value) pairs; repeated names are
    combined into one comma-separated value in the order given.
    """
    combined = {}
    for name, value in headers:
        lname = name.lower().strip()
        value = " ".join(str(value).split())
        combined.setdefault(lname, []).append(value)
    lines = []
    for lname in sorted(combined):
        lines.append("%s:%s\n" % (lname, ",".join(combined[lname])))
    return "".join(lines), ";".join(sorted(combined))


def _sigv4_sign_canonical(canonical_request, access_key, secret_key, amz_date,
                          region="auto", service="s3", signed_headers=None):
    """Sign a pre-built canonical request. Returns (authorization_header, scope).

    Split out from _sigv4_sign so the signing chain can be validated directly
    against the official AWS SigV4 test suite vectors.
    """
    datestamp = amz_date[:8]
    scope = "%s/%s/%s/aws4_request" % (datestamp, region, service)
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                hashlib.sha256(canonical_request.encode()).hexdigest()])

    def _sign(key, msg):
        return hmac_digest(key, msg)

    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac_digest(k_signing, string_to_sign).hex()

    if signed_headers is None:
        # canonical request layout: METHOD \n URI \n QUERY \n HEADERS \n\n SIGNED \n PAYLOAD
        signed_headers = canonical_request.rsplit("\n", 2)[-2]
    auth = ("AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
            % (access_key, scope, signed_headers, signature))
    return auth, scope


def _build_canonical_request(method, url, payload_sha256, extra_headers=None,
                             sign_headers=None, raw_path=None, raw_query=None,
                             s3_style=False, amz_date=None):
    """Build the SigV4 canonical request string.

    Single source of truth for canonicalisation -- both _sigv4_sign and the
    test harness use this, so the harness validates the real code path.
    Returns (canonical_request, signed_headers).
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.netloc

    available = {"host": host, "x-amz-content-sha256": payload_sha256}
    if amz_date:
        available["x-amz-date"] = amz_date
    for k, v in (extra_headers or {}).items():
        available[k.lower()] = v

    if sign_headers is None:
        sign_headers = ["host", "x-amz-content-sha256", "x-amz-date"]
    sign_headers = sorted(h.lower() for h in sign_headers)
    canonical_headers = "".join("%s:%s\n" % (k, available[k]) for k in sign_headers)
    signed_headers = ";".join(sign_headers)

    path = raw_path if raw_path is not None else parsed.path
    query = raw_query if raw_query is not None else parsed.query
    canonical_path = _canonical_uri(path, s3_style=s3_style)
    canonical_query = _canonical_query(query)

    canonical_request = "\n".join([method, canonical_path, canonical_query,
                                   canonical_headers, signed_headers, payload_sha256])
    return canonical_request, signed_headers


def _sigv4_sign(method, url, payload_sha256, access_key, secret_key,
                amz_date, region="auto", service="s3", extra_headers=None,
                sign_headers=None, raw_path=None, raw_query=None, s3_style=False):
    """Return (authorization_header, scope) for an AWS SigV4-signed request.

    `extra_headers` is a dict of additional header values available to sign
    (e.g. {"range": "bytes=0-9"}).
    `sign_headers` is the ordered list of header names to include in the
    canonical (signed) set. Defaults to host, x-amz-content-sha256, x-amz-date
    (what R2/S3 PUT requires). Header names are lowercased and sorted per spec.
    `raw_path`/`raw_query` override the path/query taken from `url` (used by
    the AWS test-suite validation harness).
    `s3_style` keeps the canonical path verbatim (S3/R2 behaviour).
    """
    canonical_request, signed_headers = _build_canonical_request(
        method, url, payload_sha256, extra_headers=extra_headers,
        sign_headers=sign_headers, raw_path=raw_path, raw_query=raw_query,
        s3_style=s3_style, amz_date=amz_date)
    return _sigv4_sign_canonical(canonical_request, access_key, secret_key,
                                 amz_date, region=region, service=service,
                                 signed_headers=signed_headers)


def _sigv4_headers(method, url, payload_sha256, env, region="auto"):
    """AWS SigV4 headers for the R2 S3-compatible endpoint.

    R2 speaks the S3 dialect, so the canonical path is used VERBATIM
    (not normalized, not re-encoded) -- matching botocore's S3SigV4Auth.
    """
    import datetime
    amz_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    auth, _scope = _sigv4_sign(method, url, payload_sha256,
                               env["CLOUDFLARE_R2_ACCESS_KEY_ID"],
                               env["CLOUDFLARE_R2_SECRET_ACCESS_KEY"],
                               amz_date, region=region, service="s3",
                               sign_headers=["host", "x-amz-content-sha256", "x-amz-date"],
                               s3_style=True)
    from urllib.parse import urlparse
    return {"Authorization": auth, "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_sha256, "Host": urlparse(url).netloc}


def hmac_digest(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def r2_publish(local_path, filename):
    """PUT the file into R2 via the S3-compatible API (SigV4, stdlib only)."""
    env = _r2_env()
    bucket = env["CLOUDFLARE_R2_BUCKET"]
    public_base = env["CLOUDFLARE_R2_PUBLIC_URL"].rstrip("/")
    account_hint = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    endpoint = os.environ.get("CLOUDFLARE_R2_ENDPOINT")
    if not endpoint:
        if not account_hint:
            raise RuntimeError("set CLOUDFLARE_R2_ENDPOINT (e.g. https://<account-id>.r2.cloudflarestorage.com) "
                               "or CLOUDFLARE_ACCOUNT_ID")
        endpoint = "https://%s.r2.cloudflarestorage.com" % account_hint
    key = "%s/%s" % (MEDIA_SUBDIR, filename)
    public_url = "%s/%s" % (public_base, _uri_encode(key, safe="/"))

    local_size = os.path.getsize(local_path)
    local_sha = git_blob_sha(local_path)
    payload_sha = hashlib.sha256()
    with open(local_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            payload_sha.update(chunk)
    payload_sha_hex = payload_sha.hexdigest()

    # Idempotency: HEAD the public URL, compare size and first bytes.
    status, headers = http_head(public_url)
    if status == 200:
        clen = headers.get("Content-Length") or headers.get("content-length")
        if clen is not None and clen.isdigit() and int(clen) == local_size:
            head = http_range(public_url, 16)
            if head[4:8] == b"ftyp":
                log("already published: %s (identical size, %d bytes)" % (key, local_size))
                return public_url, True

    # The request path is percent-encoded; S3-style signing uses that same
    # path verbatim, so the signed path and the requested path agree.
    put_url = "%s/%s/%s" % (endpoint.rstrip("/"), bucket, _uri_encode(key, safe="/"))
    headers = _sigv4_headers("PUT", put_url, payload_sha_hex, env)
    headers["Content-Type"] = "video/mp4"
    data = open(local_path, "rb").read()
    req = urllib.request.Request(put_url, data=data, method="PUT", headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        if r.status not in (200, 201):
            raise RuntimeError("R2 PUT failed: HTTP %s" % r.status)
    log("uploaded %s to R2 (%d bytes)" % (key, local_size))
    return public_url, False


# --------------------------------------------------------------------------

def choose_filename(local_path, url_name=None):
    name = os.path.basename(local_path)
    if url_name:
        name = url_name
    name = "".join(c for c in name if c.isalnum() or c in "._-")
    if not name.lower().endswith(".mp4"):
        name += ".mp4"
    return name


def main(argv):
    backend = "github"
    args = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--backend":
            i += 1
            if i >= len(argv):
                log("error: --backend needs a value")
                return 1
            backend = argv[i]
        elif a.startswith("--backend="):
            backend = a.split("=", 1)[1]
        else:
            args.append(a)
        i += 1

    if len(args) != 1:
        log("usage: publish_media.py <local.mp4> [--backend github|r2]")
        return 1
    local_path = os.path.abspath(args[0])
    if not os.path.isfile(local_path):
        log("error: no such file: %s" % local_path)
        return 1
    if not is_mp4(local_path):
        log("error: %s is not an MP4 (no ftyp box) -- Buffer would reject it" % local_path)
        return 1

    size = os.path.getsize(local_path)
    filename = choose_filename(local_path)
    log("publishing %s (%d bytes) via %s backend" % (filename, size, backend))

    if backend == "github":
        url, existed = gh_publish(local_path, filename)
    elif backend == "r2":
        url, existed = r2_publish(local_path, filename)
    else:
        log("error: unknown backend %r (use github or r2)" % backend)
        return 1

    # The page may take a moment to exist / the CDN to refresh.
    deadline = time.time() + PUBLISH_TIMEOUT
    last_reason = "not checked"
    while True:
        ok, reason = verify_public_url(url, expect_size=size)
        if ok:
            break
        last_reason = reason
        if time.time() > deadline:
            log("error: %s did not become verifiable: %s" % (url, last_reason))
            return 1
        log("waiting for %s (%s)" % (url, reason))
        time.sleep(10)

    log("verified: 200, video/mp4, %d bytes, ftyp ok" % size)
    if existed:
        log("already published")
    sys.stdout.write(url + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as e:
        log("error: %s" % e)
        sys.exit(1)
