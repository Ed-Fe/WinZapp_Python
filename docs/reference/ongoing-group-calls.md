# Joining an ongoing group call

The conversation's Join button replaces the voice/video start buttons when WhatsApp's
`WAWebVoipOngoingCallCollection.getByChat` reports a call for that group.
The visible group is checked every ten seconds, with at most one probe in
flight. Hidden chats make no requests. Closing or changing chats invalidates
late replies and stops the old timer. Discovery never initializes VoIP or
acquires audio devices; it waits for the runtime already warmed at startup.
Failed checks hide the button without speaking. Repeated results do not
rewrite an unchanged label, and hiding a focused button moves focus to the
message field, never away from another control.

The authenticated `call/group/ongoing`, `call/ongoing` and `call/group/join` endpoints live in
`callController.ts`; the serialized native adapter is `groupCallRuntime.ts`.
The latter calls `joinOngoingWAWebVoipGroupCallPN` with the same options as
WhatsApp's Calls-tab Join button (`joinAndAccept: true`, `CALL_LOG`), after
checking the call id, chat, media type and absence of another active/pending
call. It requires the selected call to reach the live call store and never
falls back to an offer or retries the join. Failed cleanup cannot disable
the bridge of a different call. Python reuses the existing cancellable
call worker and audio lifecycle. Video is joined with the camera off, using
the same camera probe as Answer without video.

The native function signatures and collection fields were read from the
installed WhatsApp Web runtime on 2026-10-25. On 2026-10-27 the user reported
a successful real group-call join. On the same day the user completed the
preview acceptance pass and confirmed that the requested calling features
worked, including the connected-participant list. Synthetic tests exercise
the native shapes and lifecycle edge cases independently.
This does not add a participant picker to start
calls in groups exceeding the existing 32-member starting-call limit.
The visibility checks use the existing free function
`core.conversation_view.conversation_in_view(panel)`, not a method on
ConversationsPanel. Tests expose wx's actual `IsShown()` interface on both the
panel and its detail pane; inventing a `conversation_in_view` stub method hid
the first preview's opening-group crash. Poll, completion and click all use
the shared helper, including when only the detail pane is hidden.

## Ongoing filter in Calls

Calls (Alt+6) adds Ongoing after the existing All/Missed/Declined/Answered
filters. Native ongoing calls also appear first in All, with duplicate
history records suppressed. Stored call-log outcomes never make a call
joinable. `ui/ongoing_calls.py` polls the authenticated list endpoint only
while Calls is visible, at most once per ten seconds with one request in
flight; hide/show invalidates old replies. Unchanged rows are not rewritten,
row identity preserves selection, and periodic discovery does not take focus.
Failure clears joinable state and shows an unavailable placeholder on Ongoing.
Locked chats are filtered again on render and activation.

The native collection also holds multiparty calls filed under a person's
chat. Listing uses `getModelsArray()` after `checkOngoingCalls()`, and verifies
`WAWebMsgGetters.getIsGroupCall()` for a non-group chat. The same native
`joinOngoingWAWebVoipGroupCallPN` accepts that chat: its native implementation
explicitly branches on `chat.id.isGroup()`. Phone `@c.us`/`@s.whatsapp.net`
forms address the same native chat; LIDs remain distinct. Python preserves
the chat peer even when the multiparty call has no group JID.

Enter, the Join button and the context menu join the selected live call by
id. A call already active on this PC instead offers Return to call, which
focuses the existing call window without joining again. Other active calls
retain the existing busy protection. Video joins still start with the camera off.
An established local one-to-one call is included from the active call record,
even though WhatsApp's joinable collection contains multiparty calls. It offers
Return to call only; a pending outgoing attempt is never presented as connected.

On 2026-10-27 a read-only invocation of the new native list adapter returned
the user's current group call and matched its id to the active call store.
No join, hang-up or audio-device operation was invoked by that check. A
live multiparty call outside a WhatsApp group was not separately captured by
the development smoke test; that path is covered by native-shaped tests.

## Connected participants

The active call window and a selected ongoing Calls row expose Call participants.
The authenticated `call/participants` endpoint is read-only and shares the
non-initializing discovery path. For multiparty calls it uses the native
ongoing call's `callParticipants` and the exact
`WAWebVoipWaCallEnums.CallParticipantState.Connected` value, matching the filter
in `WAWebCallLogJoinOngoingCell.react`. Invited, ringing and departed people
are excluded; group membership is never used as a fallback. An established
local one-to-one call can name its peer and the local user from the active
call model. Unknown group roster shapes fail instead of claiming completeness.

The native LID-to-phone bridge deduplicates addresses when available. Saved
contact names take precedence over native profile names; raw JIDs never reach
the list. The standard wx dialog refreshes every ten seconds only while open,
also offers Refresh, keeps selection by participant identity, and does not
rewrite unchanged rows or announce periodic updates. Closing invalidates
pending results. Failure clears stale names; an ended call clears the list
and stops polling. Locked-call permission is checked before opening and again
before presenting an asynchronous response. No audio device or join is invoked.
On 2026-10-27 the named native Connected enum was verified in the running
session. There was no current call at that development check. The user later
confirmed that the button remained disabled until the call was answered and
then opened the participant list successfully. This is user-reported manual
acceptance; native-shaped synthetic tests cover membership filtering.
