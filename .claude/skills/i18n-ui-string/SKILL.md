---
name: i18n-ui-string
description: Add, change or remove a user-facing string in WinZapp. Use whenever a change introduces text a person can read or a screen reader can speak — dialog titles, buttons, menu items, list labels, error boxes, notifications, tooltips — or whenever a file under client/languages/ needs editing. Covers every registered locale (language_map.json), the mnemonic and placeholder rules, and the tests that enforce them.
---

# Adding a user-facing string

`I18n.t()` is `translations.get(key, key)` (`client/core/i18n.py`): no
fallback to another locale. A missing key is not an error — the screen reader
speaks the raw key name. So **a key added anywhere is owed by every locale
file listed in `client/languages/language_map.json`, in the same change.**

## Procedure

1. Name the key in English `snake_case`, after its role
   (`status_reply_send`, not `send_button_2`).
2. Add it to every locale file in `client/languages/` with a real
   translation; a blank value fails the suite.
3. Call it as a literal: `i18n.t("my_key")`. A key built at runtime escapes
   the static check.
4. Run the tests below.

## Rules

- **`&` is a wx mnemonic.** A literal ampersand is written `&&`. Which letter
  carries the mnemonic is each locale's choice.
- **Placeholders match across locales.** Every string goes through
  `str.format()`; a dropped `{name}` loses information, an invented one
  raises `KeyError`.
- **Reuse the locale's own vocabulary — no test catches this.** Grep the file
  for the term it already uses for the concept (pl says `czat`, en-US
  "chats", pt-BR "conversas") and use it. Where a file is mixed, match the
  strings of the same feature. Changing an established term is a native
  speaker's decision, never a side effect. See
  `docs/reference/i18n-terminology.md`.
- **The string is spoken.** Titles and list items show a contact or group
  name, never a raw JID.

## Verify

```
uv run pytest tests/test_language_files_in_sync.py tests/test_i18n_keys_exist.py
```

The first compares the locale files with each other (keys, blanks,
mnemonics, placeholders); the second checks every literal `i18n.t("...")` in
`client/**/*.py` exists.

## Adding a locale

Drop `<code>.json` into `client/languages/` and add
`"<code>": "<Display Name>"` to `language_map.json` (dict order is the
Settings combobox order). Tests derive the locale list from the map — never
write the list out in a test.
