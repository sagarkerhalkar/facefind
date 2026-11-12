# FaceFind – project log

A record of the conversation in which this app was planned and built
(11–12 November 2026). Mail addresses, network addresses and company names
are left out on purpose, because this file is stored on GitHub.

## The goal

A web app like Photomall / Picsdrop for a very large photo collection spread
over many drives:

- A guest shares a live selfie and an email address.
- The guest gets an OTP on that email, sent from the owner's own mail id.
- The guest sees a preview of only their own photos and can download them.
- Nobody else can download those photos.

## What was asked, and what was done

| # | Request | What was done |
|---|---------|---------------|
| 1 | Build the app, "full authority" | First version: Flask app with email OTP, camera selfie, face matching (OpenCV YuNet + SFace), watermarked previews, expiring download links tied to the verified browser, approval mode, activity log, admin page |
| 2 | Photos from multiple drives | An album can list many folders, one per line, from any drive. A drive that is switched off keeps its place in the index |
| 3 | Put it on GitHub | Code pushed to the `facefind` repository (the owner created the empty repository first) |
| 4 | Busy ports, LAN, VLAN, HTTPS on LAN, domain later | Automatic next-free-port, built-in HTTPS with a self-made certificate, `ALLOWED_NETWORKS` and `ADMIN_NETWORKS` lists for VLANs, `PUBLIC_URL` for a domain later |
| 5 | Commands to run it on the D: drive | `git clone` into `D:\facefind`, then `.\start.bat` (PowerShell needs the `.\`) |
| 6 | Install failed with an SSL error | Network corrupted a download. Installer now retries and stops with a clear message |
| 7 | Setup must be interactive, 6-character password | `setup.bat` asks questions and writes the settings; admin password minimum is 6 |
| 8 | "Not opening", make it easy | The app opens the admin page by itself and offers to trust its certificate on the PC |
| 9 | Scan failed: face model file could not be read | The model download was interrupted earlier. Start now checks the model files and downloads them again if damaged |
| 10 | Admin page is hard to understand | Page rewritten as Step 1 (add album) and Step 2 (share link), with a folder picker, automatic scan, live status and a Copy link button |
| 11 | Paste a Google Drive link, including folders with editor access | Drive folder links accepted. The app reads Drive through a service-account "robot" email that the folder is shared with, and copies the photos to `data/drive` |
| 12 | Google says "Service account key creation is disabled" | The Google account belongs to an organisation with that policy. Create the project with a personal Gmail account instead, or have the organisation admin lift the policy for the project |
| 13 | OTP mails are not sent | `testmail.bat` added. It showed (a) a normal password was entered instead of a 16-letter app password and (b) the network closes connections on the mail ports |
| 14 | An easy way for mail | `mail.bat` added: changes only the mail settings and tests them at once. New option "Brevo" sends over normal HTTPS when the office network blocks mail ports |

| 15 | Password "6 characters, not more" | Briefly set to exactly 6; the owner's own password had 8, so it was set back to "6 or more" |
| 16 | What does "app password has exactly 16 letters" mean | Explained: Google needs a 16-letter code it generates itself, not the normal mail password |
| 17 | Selfie returned "0 photos of you" although the owner's photos are in the album | Match strictness lowered from 0.40 to the model's published 0.363. Each selfie attempt now logs the best score and how many faces the album holds, and an unscanned album says so instead of "0 photos" |

| 18 | Selfie limit reached; allow a face photo if the selfie does not work; stop guessing | Guests can upload a face photo as well as take a selfie; limit raised to 30 tries; admin page has "Test a face photo" per album that shows the exact scores; at start the app checks the face engine and rescans any album that has no scanned photos |

## Files a user runs

| File | Purpose |
|------|---------|
| `start.bat` | Install (first time), check face models, start the app |
| `setup.bat` | Ask all setup questions again |
| `mail.bat` | Change only how OTP mails are sent |
| `testmail.bat` | Send a test mail and show what is wrong if it fails |

## Known limits (stated during the conversation)

- "100% security" does not exist. The app locks photos to a verified email,
  uses expiring links, rate limits and a log of every action.
- No automatic liveness check. Someone could show another person's picture
  as the selfie; approval mode ("Ask me first") is the protection.
- One computer, albums up to a few lakh photos each. Crores of photos need
  GPU scanning and a vector database.
- The LAN certificate is self-made, so other devices show a browser warning
  once until the app is hosted on a domain.
- Google Drive photos are copied to the local disk, which needs free space.
- Face signatures are biometric personal data: take consent, tell people,
  and close albums when the job is done.

## What was tested, and what was not

- Tested automatically (`python tests/test_flow.py`): OTP rules, session and
  download-link rules, only-own-photos rule, approval mode, multiple drives,
  offline drive, Drive-link sync with a stand-in for Google, VLAN lists,
  certificate, busy-port fallback.
- Not tested in the build environment (no internet there): real face
  matching with the model files, real Google Drive access, real mail
  sending, and the Windows-only steps (browser auto-open, certificate trust).
  These are confirmed only when they run on the owner's PC.

## Open items

1. Get OTP mail working on the office network (app password or Brevo).
2. First real scan and selfie match on a small folder, to check accuracy.
3. Connect Google Drive with a key file from a personal Google account.
4. Optional later: "Sign in with Google" instead of the key file, QR code for
   the guest link, hosting on a domain.
