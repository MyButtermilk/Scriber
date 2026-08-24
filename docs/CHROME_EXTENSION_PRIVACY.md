# Scriber for YouTube - Privacy Policy

Effective date: 2026-08-24

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

The extension does not read browser history, cookies, authentication data,
form data, private messages, video or audio content, or transcript content.

## How the data is used and transferred

The public video ID and optional visible title and channel name are used only to
construct a local `scriber://youtube/transcribe` link after an explicit user
action. The operating system passes that link to the Scriber desktop
application on the same computer.

The extension does not send data to the developer, analytics services,
advertising services, or any other remote server. After the local handoff,
Scriber processes the requested video according to the user's Scriber settings,
which may include retrieving YouTube captions or using a transcription provider
selected by the user. That processing is performed by Scriber, not by the
Chrome extension.

## Storage and retention

The extension stores no browsing data, video metadata, identifiers, analytics,
or user settings. It has no developer-operated backend and retains no data.

## Permissions

- `activeTab`: allows the toolbar popup to identify the current YouTube video
  after the user invokes the extension.
- YouTube-only content-script matches: allow the in-page Scriber action to be
  shown on supported YouTube pages. No broad host or local-network permission is
  requested.

## Sharing, advertising, and sale

The extension does not share or sell user data and does not use data for
advertising, credit decisions, or profiling. No human reads user data handled
by the extension.

The use of information received from Chrome APIs adheres to the Chrome Web Store
User Data Policy, including the Limited Use requirements.

## User control

No video is handed to Scriber without the user's explicit click. Users can stop
using the extension at any time by disabling or uninstalling it in Chrome.

## Changes and contact

Material changes to these practices will be disclosed before new data handling
begins. Questions or privacy requests can be submitted through the public
[Scriber issue tracker](https://github.com/MyButtermilk/Scriber/issues).
