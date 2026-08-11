# changelog.d

One file per release, `<version>.html`, committed in the same change as the
`VERSION` bump. Each holds the release's changelog entry — the same
hand-written `<article class="entry" data-version="…">` markup the landing
changelog page uses. When `propose-release.yml` opens the release PR in
`mcp-servers`, `scripts/render_release.py` there transcribes these fragments
into `landing/mtg/changelog/index.html` and the teaser on
`landing/mtg/index.html`, so the PR opens with the release checks passing.

Beyond the page's markup contract, a fragment may carry:

- `<p class="entry-teaser">…</p>` — shorter prose for the landing-page
  teaser. Stripped from the rendered entry; the full body is used if absent.
- `data-group="Group Name"` on a `tool-tag` span — places a **new** tool into
  that existing group on the landing page. Stripped from the rendered entry.
- `<version>.group.html` — a complete `<div class="tool-group">` block
  (icon, name, description, tags) for a release that introduces a new group.

Multiple `<article>` blocks in one file are fine (1.1.0 shipped two); the
first one feeds the teaser. Dates are typed by hand. CI fails a push whose
`VERSION` has no matching fragment here.

Files for versions that already have their entry on the landing page are
skipped by the renderer — history lives on the page; these are the inbox.
