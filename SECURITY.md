# Security policy

DiffStory reads your code and passes it to a model, so its safety checks matter as much as its drafts.

## Reporting a vulnerability

Please report security problems privately through [GitHub's private vulnerability reporting](https://github.com/yuvraaj2002/DiffStory/security/advisories/new). Don't open a public issue for them.

You can expect a first reply within a week. Once a fix ships, you'll be credited in the release notes unless you'd rather not be.

## What counts as a vulnerability

- A way to make DiffStory read a file it should exclude, such as `.env` files, keys or credential files
- A way to get repository content past the redaction step unredacted, for a secret format the scanner already claims to cover
- Repository content (a commit message, file or comment) that makes the skill write, stage, commit or run anything
- A Git call that can be made to run an external program, pager or diff tool
- A draft that gets saved with a secret in it

**A secret format the scanner doesn't know yet** isn't a vulnerability. Open a [scanner issue](https://github.com/yuvraaj2002/DiffStory/issues/new?template=scanner.yml) for it instead.

## Never include real secrets

Don't put real keys or tokens in reports, issues or test cases, even revoked ones. Use a fake value with the same format. The tests assemble fake secrets at runtime for the same reason.

## Supported versions

Only the latest release gets security fixes.
