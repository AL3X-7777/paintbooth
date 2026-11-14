# Paintbooth documentation

The documentation for people working on Paintbooth's code. For *using* Paintbooth, see the
[project README](../README.md) and the tutorials inside the app (**File > Tutorials**).

| Page | What it covers |
|---|---|
| [Architecture](architecture.md) | How the program is put together: the layers, the export pipeline, threads, where state lives |
| [File formats](file-formats.md) | Every file Paintbooth reads or writes: .pb/.ms2, template.xml, PMD, TOBJ, DDS, HashFS, .scs |
| [Security model](security.md) | Which inputs are untrusted and how each one is checked |
| [Development](development.md) | Setting up, running the tests, the quality gates, regenerating these docs, releasing |
| [Module reference](reference/modules.md) | Every module and what it is for *(generated)* |
| [Build actions](reference/build-actions.md) | Every Mods Studio 2 build action the template engine runs *(generated)* |
| HTML API reference: `docs/api/index.html` | Every module, class and function, with a search box *(generated, see below)* |

## The HTML API reference

`docs/api/` is built from the docstrings in the code, so it is always as current as the code itself.
It isn't stored in git. Each GitHub release has it as `Paintbooth <version> docs.zip`, and you can
build it yourself offline:

```
pip install pdoc
python tools/gen_docs.py
```

Then open `docs/api/index.html`. The search box (top left) finds any module, class, function or word
in a docstring.

## Keeping the docs current

The two pages marked *generated* and the HTML reference come from the code. Run
`python tools/gen_docs.py` after a change. CI runs `python tools/gen_docs.py --check` and fails when
the generated pages are out of date. The other pages are written by hand: update them when you
change how something works, not when you change a detail.
