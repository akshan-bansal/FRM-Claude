# Verifying signed TradeCard releases

Every `v*` tag on this repo produces a GitHub Release with Python
artifacts (wheel + sdist) signed by [sigstore](https://sigstore.dev)
using [cosign](https://github.com/sigstore/cosign)'s keyless mode. There
are no vendor-managed signing keys — signatures land in the public
[Rekor](https://docs.sigstore.dev/rekor/overview/) transparency log via
GitHub Actions' OIDC identity.

This is the mechanism that makes the zero-vendor-infra architecture
(`NEXT_SESSION.md` §11) trustable: nothing about the release depends on
a vendor-controlled key, and the transparency log lets a customer prove
that a given artifact was signed by *this* repo's Actions and not
tampered with in transit.

## Install cosign

```
brew install cosign                        # macOS
# or download the binary from https://github.com/sigstore/cosign/releases
cosign version
```

## Verify a downloaded artifact

Every release attaches an `<artifact>.sigstore.json` bundle beside its
`.whl` / `.tar.gz`. Download both, then:

```
cosign verify-blob \
  --bundle trading_live_claude-1.0.0-py3-none-any.whl.sigstore.json \
  --certificate-identity-regexp 'https://github.com/akshan-bansal/FRM-Claude/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  trading_live_claude-1.0.0-py3-none-any.whl
```

Successful output ends with `Verified OK`. Anything else — refuse the
artifact and open an issue. That's the whole trust story.

## Verify from the transparency log

The Rekor entry for each release artifact is queryable at
<https://search.sigstore.dev/> by any of:
- the artifact's sha256,
- the certificate identity (regex above),
- the workflow run URL.

If any of those don't match a Rekor entry, the artifact wasn't produced
by this repo's release workflow.

## For firmware images (future — sub-obj 6 follow-up)

When the ESP32 firmware build lands in CI, its `.bin` will be signed the
same way. The card's OTA path calls `cosign verify-blob` against the
image before it swaps partitions; a signature that doesn't verify
refuses the update — including a compromised GitHub token cannot ship a
malicious firmware update the card would install, because the
transparency log will show a signature that doesn't match a legitimate
workflow run.
