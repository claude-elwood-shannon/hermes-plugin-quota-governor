# Showcase decision: why this repo is public (2026-09-10)

This repository is the public face of a self-governing agent workspace.
On 2026-09-10 it was audited and flipped from private to public after the
following privacy gate, which every future repo must pass before
publication:

1. **History scan, not just HEAD.** `git log --all -p` and a
   blob-level scan of every object. Two deployed-only docs
   (`docs/disaster-recovery.md`, `BOOTSTRAP.md`) had been added and
   deleted in normal commits — the delete left the content in history.
   They carried the backup bucket location and the credential map, so
   history was rewritten with `git filter-repo` (paths dropped,
   `/home/<user>` → `~`, `/data/git` → generic, bare username → `host`)
   and the old refs force-replaced. Deletion from HEAD is not removal.
2. **No host identifiers.** No absolute host paths, usernames, bucket
   names, or endpoints in any reachable blob or commit message.
3. **Honest badges only.** The tests badge reflects a real GitHub
   Actions workflow running the same suite that passes locally
   (1,079 tests); tests whose subject is a deployed host asset skip on
   CI and stay loud on the host (`QUOTA_GOVERNOR_EXPECT_DEPLOYED=1`).
4. **Live systems verified after the flip.** The production tick
   resolves `PLUGIN_DIR` portably and was smoke-tested end to end
   (decision log + `quota_tick` observation row) from its deployed path.

Rule of thumb: *publish = privacy filter first, then tell the story.*
The calibration notes, per-model cost matrix and design records in
`docs/` are the differentiator — real measurements nobody else
publishes. They are public because they were written to be.
