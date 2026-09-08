# Local Markdown preview dependencies

These browser bundles are pinned and checked into the application so the
preview never depends on a runtime CDN.

- `marked.min.js` — marked `18.0.11`, MIT; package: <https://www.npmjs.com/package/marked>, source: <https://github.com/markedjs/marked/releases/tag/18.0.11>
- `purify.min.js` — DOMPurify `3.4.14`, Apache-2.0/MPL-2.0 dual license; package: <https://www.npmjs.com/package/dompurify>, source: <https://github.com/cure53/DOMPurify/releases/tag/3.4.14>

The corresponding license texts are next to the bundles. SGO runs marked with
`breaks: true` for legacy analysis line breaks, then passes the result through
DOMPurify with a small HTML/protocol allowlist before assigning it to the DOM.
