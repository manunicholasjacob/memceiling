# Release checklist

Everything in this file is a manual step for Manu. None of it has been done.
No external action has been taken on this repository: nothing has been
pushed, no remote repository exists, nothing is published, no issue or pull
request has been opened, and no account has been touched. The only git
activity is a local `git init` and local commits.

## State as shipped

| item | state |
| --- | --- |
| working code | yes, library, CLI and Action |
| test suite | yes, passing locally; see the report for the output |
| local git repository | initialised, with local commits |
| pushed to GitHub | **no** |
| GitHub repository created | **no** |
| published to PyPI | **no** |
| tagged | **no** |
| Zenodo DOI | **no** |
| name availability | `membw` was free on PyPI and `manunicholasjacob/membw` was free on GitHub when checked on 1 Nov 2026 |

## Before anything else: confirm the name is still free

Checked 1 Nov 2026 and both were free, but neither is reserved, so confirm
again at release time.

1. PyPI: open <https://pypi.org/project/membw/> and confirm it is a 404.
   A 404 on the JSON API means the name is not currently registered; PyPI
   separately blocks re-registration of some previously deleted names, and
   the only definitive test is the upload itself. Upload to TestPyPI first
   for that reason.
2. GitHub: confirm <https://github.com/manunicholasjacob/membw> is still
   free. Other users have repositories called `membw` (none is a packaged,
   published tool), so the name is not distinctive on GitHub, but it is not
   taken in your namespace.

If either is gone, the fallbacks checked as free on PyPI on the same date
were `membench`, `membandwidth` and `stream-benchmark`. Renaming touches
`pyproject.toml` (`name`, URLs), `src/membw/version.py` only if the import
path changes, `CITATION.cff`, `README.md`, `CHANGELOG.md`, `action.yml`
and the directory name.

## 1. Decide on the paid-work gate

Standing rule: no bounties, cash prizes, honoraria or payouts until an
attorney clears it. Releasing MIT-licensed software and accepting no money
for it does not engage that rule, but if a sponsorship button, a bounty
platform or any paid support offer is ever attached to this repository, that
decision goes to the attorney first. Keep GitHub Sponsors off.

## 2. Final local checks

```sh
cd membw
python -m pytest -v
LC_ALL=C grep -rn '[^ -~]' .     # must print nothing
python -m membw.cli info
python -m membw.cli schema
python -m membw.cli run --json --out /tmp/membw.json
python -m build
python -m twine check dist/*
```

Confirm by eye:

- `src/membw/version.py`, `CITATION.cff` (`version:` and `date-released:`)
  and the `CHANGELOG.md` heading all say the same version. A test asserts
  the first three agree, but the release date in `CITATION.cff` is not
  checked by anything.
- No file or commit message carries an assistant co-authorship trailer or a
  tool-generated-by line. This checks it:
  `git log --format=%B | grep -icE 'co-auth|generat'` should print `0`.
- `git log` author is you.

## 3. Create the GitHub repository and push

Not done, and not to be done by any agent.

```sh
# create manunicholasjacob/membw as a PUBLIC repository in the web UI,
# with no README, no .gitignore and no licence, so the local history is
# the initial history
git remote add origin git@github.com:manunicholasjacob/membw.git
git branch -M main
git push -u origin main
```

Then in the repository settings:

- Set the description and the topics: `memory-bandwidth`, `roofline`,
  `benchmark`, `stream`, `provenance`, `reproducibility`, `arm`,
  `big-little`, `performance`.
- Leave Discussions off for now. Leave Sponsors off, per step 1.
- Confirm the Actions tab shows the CI workflow and let it run on `main`
  before tagging.

## 4. Watch the first CI run

The matrix covers Linux, Windows and macOS. Expect this and do not treat it
as a failure:

- The `self-test-action` job will usually report a non-`PASS` verdict. A
  shared GitHub runner is noisy, its SMBIOS is normally not readable from an
  unprivileged process, and there is no governor to read on Windows or
  macOS. `fail-on-invalid` is `false` precisely so that this records rather
  than breaks. Read the job summary and confirm the reasons are the ones you
  would expect, which is itself the evidence the refusal logic works on real
  CI.
- `build-c` may fail on macOS or Windows runners. It is wrapped so a missing
  toolchain falls back to the numpy path with the figure labelled.

If CI shows something other than those, fix it before tagging.

## 5. Tag v0.1.0

The Action must be referenceable as `manunicholasjacob/membw@v0.1.0`, so the
tag is what makes the Action usable by a dependent.

```sh
git tag -a v0.1.0 -m "membw 0.1.0"
git push origin v0.1.0
```

Then create the GitHub release from that tag, with the `0.1.0` section of
`CHANGELOG.md` as the body. Consider also moving a `v0.1` floating tag, but
only after `v0.1.0` is proven; many consumers pin the full version and a
floating tag is a maintenance commitment.

## 6. Publish to PyPI

```sh
python -m build
python -m twine upload --repository testpypi dist/*
# install from TestPyPI into a clean virtualenv and run:
#   membw info
#   membw build-c
#   membw --json
python -m twine upload dist/*
```

Use an API token scoped to the project. Do not put the token in any file in
this repository.

## 7. Optional: list the Action on the Marketplace

GitHub offers this from the release page once `action.yml` is at the root,
which it is. The Marketplace listing is not required for the "Used by"
dependency graph, so decide on it separately.

Note that two separate GitHub surfaces are involved, and shipping both is
why the repository has both a package and an Action:

- the package dependents list, populated when another repository names
  `membw` in `pyproject.toml` or `requirements.txt`;
- the Action dependents list, populated when another repository names
  `manunicholasjacob/membw` in a `.github/workflows` file.

Neither is populated by a bare CLI.

## 8. Optional: archive for a DOI

If this is to be citable, mint a Zenodo DOI from the GitHub release, then
add the `doi:` and `identifiers:` fields to `CITATION.cff` and the badge to
`README.md`, and record the DOI in the artifacts ledger alongside the other
Zenodo deposits.

## 9. Things deliberately left undone

- **No Linux verification.** Everything in this repository was written and
  tested on Windows 11 with MSVC. The Linux readers (`/proc/cpuinfo`,
  `/sys/devices/system/cpu/*`, `/proc/stat`, `/proc/vmstat`,
  `/sys/fs/cgroup/cpu.stat`, `dmidecode`, the device tree, `vcgencmd`) are
  exercised by the test suite against fixed text, but have not been run
  against a real Linux machine. Run `membw info` and `membw --json` on the
  Raspberry Pi 5 and on a Linux x86 box before claiming anything about them
  publicly. The first CI run gives you the Linux and macOS pass.
- **No Arm big.LITTLE verification.** The per-cluster path is the headline
  claim and has only been exercised against an x86 hybrid part's efficiency
  classes on Windows and against fixed `/proc/cpuinfo` text in the tests. It
  has not been run on a real big.LITTLE SoC.
- **No comparison run against STREAM.** Before quoting a `membw` figure next
  to a STREAM figure anywhere public, run both on the same machine in the
  same session and reconcile them. Until then, do not present a `membw`
  number as reproducing any previously published figure.
- **Two related-work details were not confirmed and the README avoids
  claiming them.** likwid-bench has an undocumented `-d <delimiter>` flag
  that may produce machine-readable output; read `bench/bench.c` before
  saying anything about likwid's output format in print. BabelStream's
  licence, output format and pinning behaviour were not read from source, so
  the README says nothing about them. lmbench's licence was not read from
  the tarball.
- **No announcement.** No LinkedIn post, no mailing list, no issue on
  another project. If a post is wanted, it goes through the usual
  guardrails: no claim of adoption, no claim that any figure here reproduces
  a published result, and the Windows caveat stated plainly.
