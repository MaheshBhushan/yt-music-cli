# Google OAuth verification — ytm

## Copy into Google Auth Platform → Branding

| Field | Value |
| --- | --- |
| App name | `ytm` |
| User support email | `mkoduri73@gmail.com` |
| Application home page | `https://ytm.mrded.info/` |
| Application privacy policy link | `https://ytm.mrded.info/privacy` |
| Application Terms of Service link | `https://ytm.mrded.info/terms` |
| Authorized domain | `mrded.info` |
| Developer contact email | `mkoduri73@gmail.com` |

Project: `ytm-music-cli-20260913` (ytm Music CLI).
Client type: Desktop app. Do not replace the existing client or change it to a Web client.
The site contains no authorization callback or credential form. Desktop authorization returns to the loopback server on the user's own computer.

Branding: https://console.cloud.google.com/auth/branding?project=ytm-music-cli-20260913
Audience: https://console.cloud.google.com/auth/audience?project=ytm-music-cli-20260913
Data access: https://console.cloud.google.com/auth/scopes?project=ytm-music-cli-20260913
Verification: https://console.cloud.google.com/auth/verification?project=ytm-music-cli-20260913

## Application description

> ytm is an independent, open-source desktop command-line and terminal-interface application for YouTube Music. It lets users search and play music, view their library and playlists, create and edit playlists, add or remove playlist tracks, and like songs. The application runs locally on the user's computer. Google handles authorization in the user's browser; OAuth tokens and local application state are stored on the user's device. Account requests go directly to Google/YouTube rather than through a developer-operated application backend.

## Current requested scope and justification

Current code requests `https://www.googleapis.com/auth/youtube`.

> ytm requests YouTube account access to display the signed-in user's library and playlist contents and execute playlist and like actions explicitly requested by the user. Implemented write operations include creating, editing, and deleting playlists, adding and removing playlist tracks, and liking songs. Read-only access does not support these writes. ytm uses the ytmusicapi library for account operations. It does not implement video uploading or channel deletion. Tokens remain on the user's device and are used for these visible music features.

This describes actual behavior; it is not a claim that Google has approved the scope. The scope is broader than the subset of actions ytm implements. Before submission, confirm with Google's review requirements whether a narrower write-capable scope would work with the supported YouTube Music flow. Do not claim that every narrower scope is incompatible without testing it. The app also uses unofficial YouTube Music interfaces and yt-dlp/mpv playback, which must be described honestly if Google asks how it works. A website cannot resolve service-policy or API-eligibility concerns on its own.

## Domain ownership — outstanding confirmation

A project owner/editor must verify `mrded.info` in Google Search Console. Cloudflare hosting alone does not establish Google ownership verification.

1. Open https://search.google.com/search-console and use the Google account that owns/edits this OAuth project.
2. Select an existing verified `mrded.info` Domain property, or add a Domain property for `mrded.info`.
3. If requested, copy Google's exact TXT verification value into a new TXT record at `@` in Cloudflare. Keep existing TXT records; do not overwrite mail or other verification records.
4. Complete Google's verification and retain the TXT record.

The current Google CLI session could not read Site Verification status (HTTP 403); ownership is not confirmed by this work.

## Consent and public availability

- Put the matching URLs above into Branding and save.
- Set the audience to External if the app should serve users outside a Workspace organization.
- Publishing the OAuth app and passing Google verification are distinct. Use Publish app when ready and complete any required verification.
- While in Testing, add approved testers under Audience → Test users. Adding someone is not a substitute for public verification.
- An unverified production app may still show warnings and remain subject to Google's user cap.
- Do not claim the app is Google-verified until the console confirms it.

## Demo video to record

Use a test account and avoid exposing credentials, tokens, private playlists, or unrelated personal data.

1. Show the homepage, privacy link, app name, and installed ytm version.
2. Run `ytm auth` from a clean test configuration. Show the entire Google authorization flow in English, including the consent screen, app name, requested permission, and privacy link.
3. Show successful return to ytm, then view library/playlists.
4. Create a temporary test playlist, add a track, edit its title, remove the track, and delete that test playlist. Show the user interaction that initiates each action.
5. Like a test track to demonstrate that write feature.
6. Show local data controls in the privacy page and how to remove ytm from Google Account connections.
7. Upload the genuine recording to a location Google's reviewers can access. Paste its URL into the verification submission. Do not use a fabricated demonstration or label a mockup as a real OAuth flow.

## Source references

- https://support.google.com/cloud/answer/13464321?hl=en
- https://support.google.com/cloud/answer/13804565?hl=en
- https://support.google.com/cloud/answer/15549945?hl=en
- https://developers.google.com/terms/api-services-user-data-policy

## Deployment

This is a static Cloudflare Workers Assets site. It does not run an app backend or collect OAuth callbacks. Only `public/` is uploaded; this checklist and npm tooling are not public assets.

```sh
cd website
npm ci
npm run check
npm run deploy
```

The root domain and other subdomains are not modified by this site's custom-domain deployment.
