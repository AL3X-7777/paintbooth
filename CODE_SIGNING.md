# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by
[SignPath Foundation](https://signpath.org).

Windows therefore shows **SignPath Foundation** as the publisher of `Paintbooth Setup.exe`.

## Team roles

| Role | Members |
|---|---|
| Committers and reviewers | [AL3X-7777](https://github.com/AL3X-7777) |
| Approvers (approve each signing request) | [AL3X-7777](https://github.com/AL3X-7777) |

Only release builds made by this repository's GitHub Actions workflow from its own source code are
signed. Every signing request is approved by hand.

## Privacy policy

Paintbooth does not collect or send any personal data. Its only network access is optional:

* **Update check:** when Paintbooth starts and the computer is online, it asks
  `api.github.com` for the latest release of this repository, to tell you if a newer version
  exists. Nothing about you or your projects is sent. Turn it off in *Settings > Appearance and
  folders*.
* **Links you click** (tutorial videos, template downloads) open in your web browser.

Everything else (projects, exports, settings) stays on your computer. Paintbooth has an
uninstaller (*Windows Settings > Apps*), which keeps your `Documents\Paintbooth` folder.
