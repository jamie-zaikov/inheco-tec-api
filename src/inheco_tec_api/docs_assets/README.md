# Vendored Swagger UI assets

The `/docs` page of the inheco-tec api serves these files from the installed package, so the lab
host never loads a script from a CDN.

| File | Source |
|---|---|
| `swagger-ui-bundle.js`, `swagger-ui.css`, `LICENSE`, `NOTICE` | npm `swagger-ui-dist` 5.33.2, Apache-2.0 |

- Tarball: `https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.33.2.tgz`
- npm integrity (checked on download):
  `sha512-MyrWX06zkJRYPnIau8Qj8F6KfzS+Qj8D9AR36vCWkkC9ts1r1DYF84apgCvZBzO2Wzm7G7loCd5VS+4JLWoyLQ==`
- SHA-256 of the files as committed:
  - `swagger-ui-bundle.js`: `6b3bfcaff3fc82c0e6992ca4b370c4f22970dec35fff60d229302b3ee7688715`
  - `swagger-ui.css`: `1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321`

The files are unchanged copies. To update them, download a new `swagger-ui-dist` tarball, check
its npm integrity value, copy the same four files, and update this table.
