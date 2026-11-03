# blog_static: shared assets for ampixa.com/blog

`build_blog_site` copies this folder to `<site>/blog/_static/` on every build. It writes `blog.css` and `blog.js`
under content-hashed names, because nginx caches css/js for 7 days. Markdown files here, including this README, are
never copied to the site.

Created 2026-11-03 by Claude Code (Opus 5.5, session on mbp2), at the user's request to apply the approved blog mock
to ampixa.com/blog. Status: active. Owner: cdjk. Next review: 2026-12-03. Delete a file only together with the
template change that stops referencing it.

| File | Role | Source | SHA256 |
|---|---|---|---|
| `blog.css` | listing + post styles, light only | written for the approved mock | in git |
| `blog.js` | listing filter, grid/map view, contents highlight, read-aloud player | written for the approved mock | in git |
| `fonts/cmu-serif-400.woff2` | body text (Computer Modern) | npm `computer-modern@0.1.3` `fonts/cmu-serif-500-roman.ttf` (sha256 cf2b28d1…), subset U+0020-024F, 0300-036F, 2000-206F, 20AC, 2190-22FF, 25A0-25FF | c3fd2208… |
| `fonts/cmu-serif-700.woff2` | bold | same package, `cmu-serif-700-roman.ttf` (82d500c7…), same subset | c9ced832… |
| `fonts/cmu-serif-400-italic.woff2` | italic | same package, `cmu-serif-500-italic.ttf` (0473b873…), same subset | 72918611… |
| `fonts/nithya-ranjana-du.woff2` | Ranjana backdrops and display titles only | `NithyaRanjanaDU-Regular.otf` v1.000 (8a2ebe62…, EkType, from mbp2 `~/github/llm/ocr-tech/data/fonts/all/devanagari/`), subset U+0900-097F, 200C-200D | 939d2355… |
| `icons/favicon.svg`, `icons/favicon-32.png`, `icons/apple-touch-icon.png` | tab and home-screen icons: the Ampixa crimson pixel cross (four squares, centre empty) on paper | drawn 2026-11-04 by make_icons.py (Claude Code); the geometry matches the cross in the Yak figures | in git |
| `fonts/LICENSE-CMU-Serif-OFL.txt` | licence, shipped with the font | npm package `OFL.txt` | n/a |
| `fonts/LICENSE-Nithya-Ranjana-OFL.txt` | licence, shipped with the font | github.com/EkType/NithyaRanjana `OFL.txt` | n/a |

Both fonts are under the SIL Open Font License 1.1: free to use, embed and redistribute, but not to sell on their
own. Reproduce a subset with `pyftsubset <ttf> --unicodes=<ranges> --layout-features='*' --flavor=woff2`.
Press Start 2P, IBM Plex Mono and Noto Serif/Sans Devanagari load from Google Fonts and are not stored here.
