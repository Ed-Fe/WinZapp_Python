# Polish terminology and UI wording

Read this file before writing or reviewing Polish `msgstr` values in
`translations/pl/LC_MESSAGES/winzapp.po`, including translations added alongside
a feature. Apply the same terms to Polish help and changelog text.

These native-speaker-reviewed rules take precedence over older help, changelogs
and unreviewed catalog wording. An AI-generated `msgstr` does not become a
reviewed term merely by being committed or passing gettext tests. Use code to
establish what a feature does; use this glossary to name that meaning in Polish.

## Contents

- [Translation workflow](#translation-workflow)
- [WhatsApp chat means czat](#whatsapp-chat-means-czat)
- [Chat access protection and contact blocking](#chat-access-protection-and-contact-blocking)
- [Other reviewed terms](#other-reviewed-terms)
- [Meaning, sources and spoken wording](#meaning-sources-and-spoken-wording)
- [Gettext and final review](#gettext-and-final-review)

## Translation workflow

1. Check this glossary for the concept, including its exceptions.
2. Search the Polish PO for reviewed wording, especially strings from the same
   feature. Do not copy a nearby unreviewed synonym over a reviewed term.
3. Check related labels, states, settings, shortcut descriptions and help.
4. If the meaning is unclear, trace the key's call sites and actual behavior.
   Check all uses when one key serves several operations.
5. Translate the meaning into natural Polish, then review technical correctness
   and terminology separately.

Preserve good existing translations and prior native-speaker corrections.
Change them for a concrete reason: incorrect meaning, unnatural wording,
inconsistency or changed behavior. Do not rewrite them merely for variety.
If a new concept is not covered here, establish its context instead of inventing
a blanket substitution rule.

## WhatsApp chat means czat

For a WhatsApp message thread, use **czat**, including when the English source
calls it a `conversation`. Inflect it normally in Polish.

| Meaning | Polish |
| --- | --- |
| chat / conversation thread | czat |
| chats / conversation threads | czaty |
| chat list | lista czatów |
| new chat | nowy czat |
| open this conversation | otwórz ten czat |
| this conversation's messages | wiadomości tego czatu |
| chat data | dane czatu |
| private chats, in descriptive prose | czaty prywatne |
| Personal, the category name | Osobiste |

Do not use `rozmowa`, `rozmowy`, `konwersacja` or their inflected forms to name
a WhatsApp message thread. An English key beginning with `conversation_` does
not make `rozmowa` the right Polish term. The `Chats` navigation button is
`Czaty`.

Do **not** ban `rozmowa` globally or run a global search/replace. It remains
valid for a spoken conversation, voice/video call or AI conversation. Examples:

- `ai_error_limit`: `Ta sesja osiągnęła limit liczby żądań lub długości rozmowy.`
  describes an AI session; keep `rozmowy`.
- `chat_encryption_notice`: `Wiadomości i rozmowy są w pełni zaszyfrowane.`
  includes calls. This WhatsApp wording was checked in the Polish phone app;
  keep `rozmowy`. The full reviewed notice is:
  `Wiadomości i rozmowy są w pełni zaszyfrowane. Tylko osoby w tym czacie mogą je odczytywać, odsłuchiwać i udostępniać.`
- `bulk_transcription_open_hint`: `otwórz ten czat` refers to opening the
  message thread; `otwórz tę rozmowę` is inappropriate here.

For automatic-processing settings, describe the action being enabled for chats.
Reviewed examples include `Czaty objęte automatycznym przetwarzaniem:`,
`Wszystkie czaty oprócz tych, w których wyłączę tę funkcję` and
`Tylko czaty, w których włączę tę funkcję`. Avoid wording that suggests the
chats themselves are switched on or off.

## Chat access protection and contact blocking

Keep chat access protection distinct from blocking a contact, encryption and
closing the local protected-chat vault.

| Meaning | Polish |
| --- | --- |
| lock chat: enable access protection | Zabezpiecz dostęp do czatu |
| unlock chat: disable access protection | Wyłącz zabezpieczenie dostępu do czatu |
| locked chats | Zabezpieczone czaty |
| locked chat, in descriptive prose | zabezpieczony czat / czat z zabezpieczeniem dostępu |
| chats without chat lock | czaty bez zabezpieczenia dostępu |
| block a contact | Zablokuj |

Do not call access-protected chats `zablokowane czaty`. Keep `Zablokuj` for
contact blocking. Check the code when `lock` refers to a vault or another
mechanism; do not replace all occurrences of that English word alike.

Reviewed explanation:
`Dostęp do tego czatu jest zabezpieczony w WhatsAppie na telefonie. Możesz to wyłączyć tylko tam.`

## Other reviewed terms

Apply these to the indicated meaning, with grammatical inflection and
capitalization appropriate to the UI location.

| Meaning / UI role | Polish |
| --- | --- |
| forward a message | Przekaż |
| forwarded message marker | Przekazano |
| last seen | Widziano |
| who can see when I am online | Kto widzi, że jestem online |
| online visibility: same as last seen | Tak samo jak w ustawieniu „Widziano” |
| group participants / members | Członkowie |
| group size | Liczba członków |
| Media tab | Media |
| frequent emoji category | Często używane |
| caption | Podpis |
| add a caption | Dodaj podpis... |
| playback device | urządzenie odtwarzające |
| mute chat | Wycisz |
| unmute chat | Wyłącz wyciszenie |
| unarchive chat action | Przywróć |
| show the window from the system tray | Pokaż |
| read-receipt status | Wyświetlono |
| announce, as a behavior setting | Ogłaszaj |

`Osobiste` is a category name; do not mechanically turn descriptive
`czaty prywatne` into `czaty osobiste`. Likewise, the `Media` tab does not
require replacing every descriptive use of `multimedia`.

Keep the reviewed phone-linking label `Połącz, korzystając z &numeru telefonu`;
the ampersand is a mnemonic. Preserve established reply terminology where it
matches the same action; check context before borrowing a term from another app.

## Meaning, sources and spoken wording

Prefer natural Polish over a word-for-word rendering. Reorder, shorten or
clarify a sentence when that preserves the full meaning. Do not invent behavior,
recovery advice or restrictions that the application does not implement.

Use Polish WhatsApp / WhatsApp Web terminology for corresponding WhatsApp
features. Use Unigram / Telegram for genuinely comparable general messaging
actions, and Windows / Microsoft terminology for system functions. Resolve
conflicts in this order: actual meaning, natural Polish, terminology of the
application whose feature is represented, then consistency within WinZapp.
Verify an official label when possible; do not present a guessed phrase as a
quotation from WhatsApp.

Make text understandable when spoken by NVDA, JAWS or Narrator without visual
context. Put information in a clear order, keep short messages unambiguous and
remove redundant wording. Match the role: a button can say `Otwórz`, a setting
can say `Ogłaszaj`, and a completed action can say `Połączono`. Do not apply
the grammar of an NVDA script description mechanically to WinZapp shortcuts.

Describe the real shortcut behavior, including state-dependent actions. Keep
key combinations intact and verify referenced settings paths in code. Use the
same UI names in help; do not create a second name to avoid repetition.

Do not translate proper names such as WinZapp, WhatsApp, WPPConnect, Meta AI,
NVDA or Windows. Natural inflection is allowed, e.g. `w WhatsAppie` and
`za pomocą Whispera`.

## Gettext and final review

Follow [the gettext workflow](gettext-migration.md) for synchronization,
compilation and validation. `translations-update` synchronizes catalogs; it
does not translate or review them. Translate new blank entries and review
changed English source before clearing `fuzzy`; never clear flags in bulk to
make a check pass. Preserve translator comments and reviewed translations.

Preserve stable keys, English sources, placeholders and format specifications
such as `{count}` and `{mb:.1f}`. Reorder placeholders only as Polish grammar
requires. Preserve meaningful newlines, markup and links. Keep wx mnemonics
valid: `&` marks an access key, `&&` is a literal ampersand; check collisions
within the relevant UI group. Polish quotes `„…”` are preferred in prose,
without changing technical syntax. Do not add gettext plural entries: the
current WinZapp API rejects them (see the gettext workflow).

Before finishing, inspect the changed Polish `msgstr` values and related
feature strings for terminology regressions. Searching stems such as `rozmow`,
`konwersac` and `zablokowan` helps find candidates; each match needs a semantic
review and can be legitimate. Do not treat matches in keys or English sources
as Polish wording errors. Fix chat-thread wording while retaining AI, call and
contact-blocking meanings.

Run the catalog checks required by the i18n skill and inspect the diff for
unrelated edits. Passing tests does not replace this language review. If Polish
shortcut descriptions change, regenerate the dependent guide with
`uv run python docs/user-guide/gerar_guia.py . pl`, inspect the resulting
`client/data/help_guide/pl.html`, and include its intended changes. Follow
[the guide maintenance process](../user-guide/PROCESSO.md); an ignored generated
guide may need an explicit `git add -f client/data/help_guide/pl.html`.
