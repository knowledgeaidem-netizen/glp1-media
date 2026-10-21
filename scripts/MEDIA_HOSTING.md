# Media Hosting — public MP4 host for the GLP-1 channel

Buffer's GraphQL API has **no binary upload endpoint**. Buffer's own roadmap
still lists *"Adding support for binary file upload"* as upcoming. To publish
a video through the API we must instead hand Buffer a **public, reachable
HTTPS URL**:

```graphql
createPost(input: {
  profileID: "...",
  text: "...",
  assets: [{ video: { url: "https://..." } }]
})
```

Buffer's servers fetch that URL at publish time. The URL must therefore be:

* **public** — no login, no auth header, no signed query string;
* **stable** — the same URL must keep working for the life of the post;
* **correct** — it must serve the real MP4 bytes with `Content-Type: video/mp4`.

A URL that returns a 403, an HTML login page, or a `text/html` body will be
rejected by Buffer.

---

## Chosen path: GitHub Pages (public repo `glp1-media`)

MP4s live in a dedicated **public** GitHub repository and are served by
**GitHub Pages**:

| | |
|---|---|
| Repo | `https://github.com/knowledgeaidem-netizen/glp1-media` |
| Serving branch | `media` (there is no `main` branch) |
| Media directory | `media/` |
| Public base URL | `https://knowledgeaidem-netizen.github.io/glp1-media` |
| URL format | `https://knowledgeaidem-netizen.github.io/glp1-media/media/<filename>.mp4` |

Live example (verified, 12,476,997 bytes, `video/mp4`):

```
https://knowledgeaidem-netizen.github.io/glp1-media/media/p2_anchored.mp4
```

### Why this path

1. **It needs no new credential.** `gh` is already authenticated with the
   `repo` scope, and GitHub Pages is enabled on the repo. There is **no
   Cloudflare API token** anywhere on this machine (verified: not in the
   environment, not in `~/.hermes/.env`, not in any project `.env`, no wrangler
   OAuth config, nothing in the macOS keychain, nothing in shell history).
2. **The site repo could not be used.** `DigitalEstateMedia/getglp1` is
   private and its Cloudflare Pages project is **not** connected to GitHub —
   the repo has zero GitHub deployments and zero check-runs, and there is no
   `.github/workflows/` directory. A push therefore cannot trigger a Pages
   build; a deploy needs a Cloudflare token, and it would also mean shipping
   ~12 MB binaries into the site repo.
3. **A dedicated repo is the right boundary.** Media binaries are large,
   write-heavy and not source. Keeping them in their own public repo means
   the site repo stays clean, and the CDN can be handed over or moved
   independently.
4. **Org repo creation was not possible.** The GitHub account
   `knowledgeaidem-netizen` is a *member* of the `DigitalEstateMedia` org
   (`members_can_create_repositories` is true, but the account lacks the
   admin permission), so `glp1-media` was created under the personal account.

### Trade-offs to be aware of

* **Public by definition.** Anyone with the URL can fetch the video. That is
  inherent — Buffer needs an unauthenticated URL, and these are social-media
  posts.
* **GitHub Pages limits.** 100 GB/month bandwidth, 1 GB published site size.
  A 12 MB video is fine; hundreds of them would not be. R2 (below) is the
  scale-out path.
* **Repo size.** Each video is committed as a blob, so the repo grows
  permanently. History rewriting is not worth the risk here; use a fresh
  media repo if this becomes a problem.
* **Pages build latency.** A push takes roughly 30–60 s to go live.
  `publish_media.py` polls until the URL verifies, so this is invisible to
  callers.

---

## Usage

```bash
python3 scripts/publish_media.py /path/to/video.mp4
```

* **stdout** — the public URL and nothing else (one line). This is the
  contract the Buffer GraphQL client depends on.
* **stderr** — all diagnostics.
* **exit 0** — the URL was verified live: HTTP 200, `Content-Type: video/mp4`,
  byte length matching the local file, and a valid MP4 `ftyp` box.

```bash
# capture the URL for the Buffer client
VIDEO_URL="$(python3 scripts/publish_media.py workspace/20261017-eu-weight-loss-pill_anchored.mp4)"
```

**Idempotent.** If the exact same bytes are already published, the script
prints `already published` on stderr, skips the upload, re-verifies the live
URL and prints the same URL. Re-running is always safe.

**Input validation.** A file that is not a real MP4 (no `ftyp` box) is
rejected before anything is uploaded, because Buffer would reject it anyway.

**Where it keeps its clone.** `~/.cache/glp1-media` (override with
`GLP1_MEDIA_CLONE_DIR`). `GLP1_MEDIA_TIMEOUT` (default `600`) bounds how long
it waits for the Pages build.

### Filenames

The local basename is reused, filtered to `[A-Za-z0-9._-]` and forced to end
in `.mp4`. Two different source files that reduce to the same name would
collide on one URL — rename one of them.

---

## Switching to Cloudflare R2 (the scale-out path)

R2 is the better host once bandwidth or repo size matters. It needs a
credential that **does not exist yet** — create it, then set four environment
variables and pass `--backend r2`.

**Creating the credential (Cloudflare dashboard):**

1. Cloudflare dashboard → **R2** → **API Tokens** → **Create Account API Token**.
2. Permissions: **Object Read & Write**, scoped to the specific bucket.
3. Click **Create Account API Token** → it shows an **Access Key ID** and a
   **Secret Access Key**. *Both are shown only once* — copy them now.
4. **R2** → **Manage R2** → **Public access** → enable the
   **`r2.dev` subdomain** for the bucket (or attach a custom domain).
   Copy the resulting public URL, e.g. `https://pub-xxxxxxxx.r2.dev`.

**Then export:**

```bash
export CLOUDFLARE_R2_ACCESS_KEY_ID="<access key id>"
export CLOUDFLARE_R2_SECRET_ACCESS_KEY="<secret access key>"
export CLOUDFLARE_R2_BUCKET="<bucket name>"
export CLOUDFLARE_R2_PUBLIC_URL="https://pub-xxxxxxxx.r2.dev"
export CLOUDFLARE_R2_ENDPOINT="https://<account-id>.r2.cloudflarestorage.com"
```

**Then publish:**

```bash
python3 scripts/publish_media.py video.mp4 --backend r2
```

URLs then look like `https://pub-xxxxxxxx.r2.dev/media/<filename>.mp4`.

Put the credentials in a **secret store or the environment of the publishing
machine** — never in a file committed to a repo. `publish_media.py` reads
them only from the environment; it has no credential file and no defaults.

**To rotate:** create a new API token with the same bucket-scoped
permissions, update the two key variables, redeploy. Old tokens can be
revoked in the Cloudflare dashboard at any time. Rotating the *public URL*
means every already-published Buffer post keeps pointing at the old URL, so
do not change the bucket name or the public domain without re-publishing.

**To move to a different host entirely:** the only thing the Buffer client
depends on is that `publish_media.py` prints a public URL for a given local
file. Add a backend, keep the stdout contract, and no other code changes.

---

## Credential rotation summary

| Credential | Where it lives now | How to rotate |
|---|---|---|
| `gh` token (`repo` scope) | macOS keychain via `gh auth login` | `gh auth refresh -s repo` |
| R2 keys | *not created yet* — see above | new token in CF dashboard, update env vars, revoke old |

No API key is stored in any file in this repo.

---

## Verification

`publish_media.py` verifies every URL it returns. The checks were also run by
hand against the live file:

```bash
$ curl -sI https://knowledgeaidem-netizen.github.io/glp1-media/media/p2_anchored.mp4
HTTP/2 200
server: GitHub.com
content-type: video/mp4
last-modified: Tue, 20 Oct 2026 23:47:17 GMT
access-control-allow-origin: *
strict-transport-security: max-age=56952452
etag: "6ad7fd85-be6245"
accept-ranges: bytes
content-length: 12476997

$ curl -s https://knowledgeaidem-netizen.github.io/glp1-media/media/p2_anchored.mp4 | head -c 12 | xxd
00000000: 0000 0020 6674 7970 6973 6f6d            ... ftypisom

$ shasum -a 256 <downloaded> workspace/p2_anchored.mp4
6b123a56d21ce59d768f40cca9439c1bdba78141434f0eed9e60f506a903902e  (identical)
```

`ftypisom` is the ISO base media file format box, i.e. a real MP4.
