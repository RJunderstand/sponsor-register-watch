# cvkit

Build path for every tailored CV and supporting statement.

```
cd cvkit
npm install docx                                  # once, in the sandbox
node cv_build.js bases/D_customer_success.json out.docx
soffice --headless --convert-to pdf out.docx      # then CHECK IT IS ONE PAGE
```

`node_modules` is not committed and must never be. It is about 9.8 MB against 32 KB of source.
Install it in the sandbox at build time, not on a local disk.

## Authority rule

The **Notion CV & Statement Block Library** is the only source of approved wording.
`blocks.json` is a build artefact of it. If the two disagree, **Notion wins and this file is wrong.**
Never introduce wording into `blocks.json` that is not in Notion or derived from a Master Profile fact.
When a block changes in Notion, change `blocks.json` in the same session and commit it.

## How to tailor

Copy the nearest `bases/*.json`, then swap only four things:

1. `title` — the advert job title, **verbatim**. This is the line a recruiter scans for.
2. `profile` — the opening sentence (`P-HE` or `P-OPS`).
3. The Skills groups that are irrelevant to this family. Delete them.
4. The section headings — mirror the person spec's own order and wording.

Do not rebuild from zero. Do not paraphrase an approved block. Copy numbers verbatim.

## Self-check

`cv_build.js` prints `clean` or a list of problems. **Never deliver a CV that did not print `clean`.**

It checks: em/en dashes in statements, `%` instead of "per cent", US spelling, hollow words,
four headline numbers on a CV, and a title line.

It **cannot** check page count. Render to PDF and look.

## Block reference

| Code | What it is |
| --- | --- |
| `H1`–`H5` | Headline numbers for the top third of a CV |
| `P-HE`, `P-OPS` | Profile opening lines |
| `PRJ-REC`, `PRJ-SORA`, `PRJ-BOOK`, `PRJ-DOC`, `PRJ-UX`, `PRJ-GOV`, `PRJ-MIRROR`, `PRJ-PARTNER` | Projects |
| `E1-HE`, `E2`, `E-EDU` | Experience and education |
| `M-VISA` | Sponsorship paragraph |
| `close: true` | Renders the approved closing line with a hyperlinked portfolio word |

Use the code and it resolves to the approved wording. Plain strings pass through unchanged.

`blocks.json` also carries:

- `facts` — address, current salary, notice period, DOB, referees. For application forms.
- `gaps` — the six things that must never be papered over. Read before writing any statement.

## Hyperlinks

CV headers carry the words `Portfolio` and `LinkedIn` as hyperlinks. Statements close with
"My portfolio is *here*." **Never print a bare URL.** Plain-text-only form fields are the
documented exception: there, write the address without the scheme.
