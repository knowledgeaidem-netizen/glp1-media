# glp1-media

Public media CDN for **getglp1.ca** (Astro site, Cloudflare Pages).

This repo exists because **Buffer's GraphQL API has no binary upload endpoint**
(Buffer's own roadmap still lists "Adding support for binary file upload" as
upcoming). Buffer's `createPost` mutation requires
`assets: [{ video: { url: "https://..." } }]` — a **publicly reachable, unauthenticated**
URL that Buffer's servers fetch at publish time.

This repository provides that host:

* MP4 files are committed under `media/`.
* They are served by **GitHub Pages** at
  `https://knowledgeaidem-netizen.github.io/glp1-media/media/<file>.mp4`
  with `content-type: video/mp4` and byte-range support.

## Publishing a new video

Use the project script (it handles the git push + Pages rebuild + verification):

```bash
python3 scripts/publish_media.py /path/to/video.mp4
```

It prints only the public URL on stdout.

## URL format

```
https://knowledgeaidem-netizen.github.io/glp1-media/media/<filename>.mp4
```

## Not a content repo

Only final, publish-ready MP4s live here. Working files, captions and drafts
stay in the private channel workspace.
