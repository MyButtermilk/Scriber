# Scriber for YouTube - Privacy Policy

Effective date: 2026-10-04 (extension 0.2.1)

## Scope and single purpose

The Scriber for YouTube Chrome extension has one purpose: let a user send the
currently open YouTube video to the locally installed Scriber desktop
application to start a transcription.

## Data handled by the extension

On supported YouTube pages, the extension locally inspects the current page URL
to identify a public YouTube video ID. When the user invokes the extension, it
may also read the visible video title and channel name. The toolbar popup uses
Chrome's `activeTab` permission to read the active tab URL and title only after
the user opens the popup.

If the user grants the optional sign-in permission in the toolbar popup, the
extension also reads cookies for https://www.youtube.com/ when handing off a
video. It filters them to YouTube domains before transmission. No other site's
cookies, browser history, form data, messages, media, or transcripts are read.

## How the data is used and transferred

The public video ID and optional visible title and channel name are used only to
construct a local `scriber://youtube/transcribe` link after an explicit user
action. The operating system passes that link to the Scriber desktop
application on the same computer.

With the optional permission, a separate one-use local HTTP handoff transfers
the YouTube session to Scriber on 127.0.0.1:8765. The app must accept the matching
video request before cookies can be uploaded. Only the official Store extension
origin is accepted. Anonymous visitor cookies cannot replace a sign-in, and a
late transfer cannot override a newer session change. Cookies and handoff capabilities
are never placed in the protocol link or URLs. The general Scriber API is not
opened to the extension. Scriber uses the session only to retrieve the selected
YouTube video's captions or audio from YouTube.

The extension does not send data to the developer, analytics services,
advertising services, or any other remote server. After the local handoff,
Scriber processes the requested video according to the user's Scriber settings,
which may include retrieving YouTube captions or using a transcription provider
selected by the user. That processing is performed by Scriber, not by the
Chrome extension.

## Storage and retention

The extension holds the one-use handoff and cookies only in memory for at most
35 seconds and does not write them to disk or extension storage. Scriber keeps
the YouTube session in memory for up to two hours or until the app closes; it
can be cleared in Scriber. No developer-operated backend receives these data.

## Permissions

- `activeTab`: allows the toolbar popup to identify the current YouTube video
  after the user invokes the extension.
- YouTube-only content-script matches show the in-page action.
- Optional `cookies` and `https://*.youtube.com/*` access allow YouTube sign-in
  handoff after explicit permission. These do not grant access to other sites.
- Optional `http://127.0.0.1:8765/*` access is limited to the local Scriber app.
  No access to other local-network hosts or broad web hosts is requested.

## Sharing, advertising, and sale

The extension does not share or sell user data and does not use data for
advertising, credit decisions, or profiling. No human reads user data handled
by the extension.

The use of information received from Chrome APIs adheres to the Chrome Web Store
User Data Policy, including the Limited Use requirements.

## User control

No video is handed to Scriber without the user's explicit click. Users may
decline/revoke the optional permissions and continue without session transfer.
They can clear the temporary session in Scriber. A later explicit video handoff
may load a new temporary session while the extension permission remains enabled;
revoke that optional permission in Chrome to stop automatic session sharing.
Users can stop
using the extension at any time by disabling or uninstalling it in Chrome.

## Changes and contact

Material changes to these practices will be disclosed before new data handling
begins. Questions or privacy requests can be submitted through the public
[Scriber issue tracker](https://github.com/MyButtermilk/Scriber/issues).
