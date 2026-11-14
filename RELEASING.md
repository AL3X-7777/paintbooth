# Releasing a new version

Short checklist. The full first-time setup (GitHub account, repository, SignPath) is in the
separate guide "Publishing Paintbooth on GitHub".

1. Make your changes and test them (`python -m unittest discover -s tests`).
2. Raise the version in `paintbooth/__init__.py`: `__version__ = "0.12.0"`. That is the only
   place; everything else reads it from there.
3. Regenerate the documentation: `python tools/gen_docs.py`.
4. Add a `## 0.12.0` section at the top of `CHANGELOG.md`. It becomes the release text on GitHub.
5. Commit and push:
   ```
   git add -A
   git commit -m "Paintbooth 0.12.0"
   git push
   ```
6. Tag the version and push the tag. This starts the release build:
   ```
   git tag v0.12.0
   git push origin v0.12.0
   ```
7. Watch it on GitHub: **Actions > Build and release**. After about 10 minutes the release appears
   under **Releases** with `Paintbooth Setup 0.12.0.exe` and the portable zip. If SignPath is set
   up, approve the signing request in SignPath when it emails you (the build waits up to 10 minutes).

Paintbooth's update check compares its own version with the newest release, so everyone with
the old version sees "Paintbooth 0.12.0 is available" the next time they start it online.

## If something goes wrong

* **The build failed:** open the failed run under Actions; the red step shows the error. Fix,
  commit, then move the tag: `git tag -f v0.12.0 && git push -f origin v0.12.0`
  (delete the half-made release on GitHub first, if there is one).
* **"Tag does not match __version__":** step 2 was missed. Fix the version, commit, move the tag.
* **Build locally instead:** on Windows with Python 3.13:
  `pip install ziglang pillow numpy` then
  `python installer/build_installer.py --python "C:\Path\To\Python313" --out dist`
