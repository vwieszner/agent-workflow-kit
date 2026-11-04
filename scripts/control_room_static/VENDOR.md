# Vendored browser assets

`.workflow/scripts/control_room.py` serves these files by an exact name allowlist (`htmx.min.js`, `app.css`).

| File | Source | Version | SHA-256 |
|---|---|---|---|
| `htmx.min.js` | https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js | htmx 2.0.4 | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |

`app.css` is this repository's own stylesheet for the control room.

To upgrade htmx, download the new `dist/htmx.min.js`, replace the file, and update the version, URL and hash above.

License: htmx is distributed under the Zero-Clause BSD license (0BSD, https://github.com/bigskysoftware/htmx/blob/master/LICENSE),
which permits use, copying, modification and distribution with or without fee and requires no notice; the
kit's MIT license does not apply to `htmx.min.js`.
