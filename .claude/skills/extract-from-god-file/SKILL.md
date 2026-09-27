---
name: extract-from-god-file
description: Pull logic out of WinZapp's big classes (MainWindow in client/main.py + client/main_window/, ConversationsPanel in client/ui/conversations.py + client/ui/conversation_panel/, StatusPanel in client/status_panel.py) without changing behaviour. Use when asked to extract, split, shrink or refactor them, when a mixin module is outgrowing its size budget, when a method is too tangled to test, or when a bug fix keeps being blocked by not being able to reach the logic from a test.
---

# Extracting from the big classes

## Where things stand

`MainWindow` and `ConversationsPanel` used to be single files of 35,600 and
18,100 lines. In September 2026 both were split **mechanically** into one
mixin module per responsibility:

- `client/main.py` (~1,900 lines: `__init__`, `init_UI`, startup) +
  `client/main_window/*.py` (29 mixins + plain-function modules)
- `client/ui/conversations.py` (~1,200 lines: `__init__`, `init_UI`,
  `refresh_labels`) + `client/ui/conversation_panel/*.py` (28 mixins + plain
  modules, including `ArchivedConversationsPanel`)

The map of each package is its `__init__.py`. `client/status_panel.py`
(~3,200 lines) was not split yet.

That split moved code; it did not make it testable. A method on a mixin is
still a method of a `wx.Frame`/`wx.Panel` and still needs a stub. So there
are now **two different jobs** this skill covers, and they are not the same
size:

1. **Extract logic from a method into a plain function** (the usual job).
2. **Split a module** that outgrew its budget, or split `status_panel.py` the
   way the other two were split (rare, mechanical, tool-driven).

## Job 1 — extract logic into a plain function

The reason is concrete: **does it turn a stub test into a direct one, or make
an untestable thing testable?** If not, you are moving code for aesthetics.

### Where it goes

- **A plain-function module in the same package** —
  `main_window/message_rules.py`, `main_window/identity_rules.py`,
  `conversation_panel/media_paths.py`, `conversation_panel/selection_rules.py`
  are the precedent: no `self`, no wx, tested directly. Add to one whose
  responsibility matches, or create a new `*_rules.py`/helper module next to
  them.
- **`client/core/`** when the slice stands alone and has its own state or
  lifecycle (`notification_manager.py`, `message_queue.py`,
  `incremental_sync.py`, `call_log.py`).

### The shape to copy

`is_countable_message()` in `main_window/message_rules.py`: type-annotated
signature, and a docstring that explains **why**, naming the incident that
motivated it. Take arguments; do not reach back into `self`. A function that
needs six attributes of `MainWindow` is telling you the slice is drawn wrong.

## Job 2 — split a module (or a remaining big file)

Use the tool that did the original split, never a hand move of hundreds of
lines:

```
python winzapp_tools/god_split/split_god_class.py <config.py>
python winzapp_tools/god_split/verify_split.py main <god file> <Class> <package dir>
python winzapp_tools/god_split/verify_imports.py
python winzapp_tools/god_split/verify_instance_access.py main
```

`verify_instance_access.py` covers what imports do not: the rest of the app
holds the instance as `self.main_window`, `self._mw`, `mw`, `panel`, … and
reaches members by string (`getattr(mw, "x", None)`). Every such name must
keep the status it had on the base ref (class member / `self.x =` attribute).

`config_main_window.py` / `config_conversation_panel.py` in the same folder
are worked examples: anchors (first method of each run → module), helper
module assignments, and `POST` hooks for the few references that cannot stay
verbatim. `verify_split.py` must report every node identical except the ones
you rewrote on purpose; list those in the commit message.

Splitting one existing mixin module in two is the same idea at small scale:
move whole methods (with the comment lines above them) into the new module's
mixin class, add it to the class bases, and run the checks below.

## Procedure (both jobs)

1. **Baseline first.** `venv/Scripts/python.exe -m pytest -q` — record the
   number. Bare `pytest` does not resolve on a dev machine here.
2. **Characterize first** (job 1): a test against today's behaviour, through
   a stub if that is the only way in (see `write-test`). It must pass before
   and after, unchanged.
3. **One responsibility per pass.** Two unrelated slices in one diff cannot
   be reverted independently.
4. **Move verbatim.** No renames, no reformatting, no drive-by fixes.
5. **Leave no copy behind.** The old method disappears or becomes a one-line
   call. Never duplicate logic.
6. **Add the direct test** the extraction made possible (job 1).
7. **Verify**: full suite, same count plus your new tests.

## Traps specific to the split layout

- **A module global is looked up where the method is defined.** Moving a
  method to another module changes which module a test must patch. Tests use
  `tests/god_modules.py`: `patch_main_global()` /
  `patch_conversations_global()` patch the name everywhere the class's code
  looks it up. Never `monkeypatch.setattr(main, "api_post", ...)` directly.
- **Source-text tests** read the whole class through `main_window_source()` /
  `conversations_source()`, or one method through
  `main_window_method_source()` / `conversations_method_source()` — never by
  slicing a file from one `def` to the next (the next method may live in
  another module).
- **A mixin never imports `main`**: running, `main` is `__main__`, so the
  import executes main.py a second time. Reach another mixin's static member
  by importing that mixin class (`ChatListMixin._counts_as_last_message`), or
  through `self`.
- **Classmethods** called as `SomeMixin.method(...)` get `cls=SomeMixin`, not
  `MainWindow`: any `cls.X` inside must be defined on that same mixin.
- **`__file__`** in a moved method now points into the package directory —
  see `_MAIN_PY` in `main_window/sending.py`.
- **Two mixins defining the same name** do not fail; the earlier one in the
  MRO silently wins. `tests/test_god_file_split_structure.py` catches it.
- **`.claude/rules/*.md` fire by path.** A trap rule that pointed at
  `client/main.py` must also name the module the code now lives in.

## What must not change

Behaviour-preserving by definition. Reviewers look first at: JID
normalization (`@lid`/phone bridge, 8/9-digit handling, fake-`@g.us` guard),
the `_live_events_ready()` gate, echo matching by message type, speech and
list mutation (`speak_output`, `Freeze()`/`Thaw()` — see `accessible-ui`),
and anything touching the locales (see `i18n-ui-string`). If making the
extraction clean seems to require changing one of these, stop: the
extraction is wrong, not the invariant.

## Tooling this repo does not have

No ruff, no mypy, no pre-commit hook, no coverage threshold. `pytest` is the
gate. Do not add one as part of an extraction — that is a team decision.

## Finishing

Report before/after `wc -l` of every file touched as raw numbers, baseline
and final test counts, the `verify_split.py` summary line when you moved
code, and what is now directly testable that was not. Then hand the diff to
`winzapp-reviewer`.
