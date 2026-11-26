# Releasing a new version

Short checklist. The full first-time setup (GitHub account, repository, SignPath) is in the
separate guide "Publishing Paintbooth on GitHub".

1. Make your changes and test them (`python -m unittest discover -s tests`).
2. Raise the version in `paintbooth/__init__.py`: `__version__ = "1.2.3"`. That is the only
   place; everything else reads it from there.
3. Regenerate the documentation: `python tools/gen_docs.py`.
4. Add a `## 1.2.3` section at the top of `CHANGELOG.md`. It becomes the release text on GitHub.
5. Commit and push to `main`:
   ```
   git add -A
   git commit -m "Paintbooth 1.2.3"
   git push
   ```
   That is all: when the tests and the build pass and there is no release for this version yet,
   GitHub tags the pushed commit `v1.2.3` and publishes the release (marked *Latest*). Pushing a
   tag `v1.2.3` yourself still works too. Pushes that don't change the version publish nothing.
6. Watch it on GitHub: **Actions > Build and release**. After about 10 minutes the release appears
   under **Releases** with `Paintbooth Setup 1.2.3.exe`, the portable zip, the source zip, the
   documentation zip and the vehicle library. If SignPath is set up, approve the signing request
   in SignPath when it emails you (the build waits up to 10 minutes).

**Earlier versions** (0.11.0 to 0.21.1, made before releases were automatic) are listed in
`.github/past-releases.txt` with the commit each was built from. **Actions > Past releases**
builds and publishes any of them that has no release (never as *Latest*); it runs when that file
changes, or by hand with *Run workflow*.

Paintbooth's update check compares its own version with the newest release, so everyone with
the old version sees "Paintbooth 1.2.3 is available" the next time they start it online.

## If something goes wrong

* **The build failed:** open the failed run under Actions; the red step shows the error. Fix,
  commit and push again; the release is made by the first run that passes. (If a release was
  already published from a broken build, delete it and its tag on GitHub, then push again.)
* **"Tag does not match __version__":** step 2 was missed. Fix the version, commit, move the tag.
* **Build locally instead:** on Windows with Python 3.13:
  `pip install ziglang pillow numpy` then
  `python installer/build_installer.py --python "C:\Path\To\Python313" --out dist`
