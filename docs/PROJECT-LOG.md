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

| 19 | Test showed "album has 0 photos and 0 faces"; scanning too slow; admin wants to test by selfie like a guest; wants to know why a scan fails | Scanner rewritten: several photos at once, large photos opened at half size, photos findable while the scan runs, speed and time left shown. Status line now gives the reason for an empty album (wrong folder, only RAW/HEIC files, unreadable files, drive offline). Admin page has "Test with my own selfie" using the camera |

| 20 | "Why download? The photos are already on Drive, scan them there." Also: not happy with the earlier work, check everything, make it work; admin test must work like the guest's; CI and tests for all platforms; phones and laptops | Whole repository reviewed and run. **Drive photos are no longer copied**: each one is read from Drive into memory at 1600 pixels (the size the scan uses), its face signatures are stored and the picture is dropped; previews and downloads pass straight from Drive to the guest; the old `data/drive` copy is deleted after the next scan. The scan starts while photos are still being found, continues after a restart if it was cut off, and re-scans are faster (missing database indexes added). The admin's selfie test now shows the matched photos, like a guest sees them. Phone layout added. GitHub Actions runs the tests on Windows, Linux and macOS |

| 21 | Screenshot: the admin test finds many photos (scores 0.60+) but the pictures do not show. Scanning still slow, "show why". A guest who attended several events should not need one link per event: sign in with email, choose a date range; the admin gives each event a date | **General link**: one link for everyone; the guest signs in once (email + code), chooses from/to dates, takes one selfie and gets photos from every open album whose date is inside the range, grouped by event. Each album has a "Date of the event" (set when creating, changeable later); albums without a date are reachable only by their own link. If any matched album has "Ask me first" ON, the request waits for approval. **Why slow**: the status line now shows the time per photo for reading and for finding faces, and how many Drive photos had to be read in full size. **Missing pictures**: the admin test now writes the reason under each photo that cannot be shown, and Drive photos scanned by the older version are shown from their old copy until the next scan. New setting `SCAN_SIDE` (1280 is about a third faster, may miss small faces). The first GitHub test run found a Windows-only fault (a preview file in use stopped the scan); fixed |

| 22 | Personal login; after login choose the date range, then the events (several), then selfie, preview, download; bookmark photos for later; no approval, but the admin must have a record of name, phone, email and which photos were downloaded; scan all photo types (JPG, ARW, HEIC ...); a selfie still shows another person's photo - "must be 100% proof" | **Sign-in** now asks name, phone and email (code goes to the email; the phone is recorded, not verified). **General link flow**: dates, tick the events, selfie, photos grouped by event. **Save** button under every photo; saved photos are kept per email and open at the next sign-in without a new selfie ("My saved photos", "Download all saved"). **New albums give photos without approval** by default (the switch is still there). **Who downloaded photos** on the admin page: name, phone, email, number of photos, the file names, and a full record file for Excel. **Photo types**: TIFF, HEIC (iPhone) and camera RAW (ARW, CR2, CR3, NEF, DNG, RAF, RW2, ORF ...) are read; on Google Drive they use Google's ready-made picture. **Wrong person**: default strictness raised from 0.36 to 0.45 and made a setting on the admin page ("Match strictness"); the admin test shows the score of every photo so the bar can be set just above a wrong match. No face system is 100% - stated again |

## Files a user runs

| File | Purpose |
|------|---------|
| `start.bat` | Install (first time), check face models, start the app |
| `setup.bat` | Ask all setup questions again |
| `mail.bat` | Change only how OTP mails are sent |
| `testmail.bat` | Send a test mail and show what is wrong if it fails |

## Known limits (stated during the conversation)

- Face matching is never 100%: look-alikes, small or blurred faces can give a
  wrong photo. Higher strictness means fewer wrong photos and more missed ones.
- Without approval and without a liveness check, a person can upload someone
  else's face picture and receive that person's photos. The download record
  shows who did it, afterwards.
- The phone number is typed by the guest and is not verified.
- "100% security" does not exist. The app locks photos to a verified email,
  uses expiring links, rate limits and a log of every action.
- No automatic liveness check. Someone could show another person's picture
  as the selfie; approval mode ("Ask me first") is the protection.
- One computer, albums up to a few lakh photos each. Crores of photos need
  GPU scanning and a vector database.
- The LAN certificate is self-made, so other devices show a browser warning
  once until the app is hosted on a domain.
- Google Drive photos are not stored on the PC, but each one still has to
  travel over the internet once to be scanned (about 0.3 MB per photo), so
  scan speed on Drive depends on the internet line.
- A folder chosen through the `G:` letter of Google Drive for desktop is
  downloaded by Windows itself; use the Drive link instead.
- Face signatures are biometric personal data: take consent, tell people,
  and close albums when the job is done.

## What was tested, and what was not

- Tested automatically (`python tests/test_flow.py`): OTP rules, session and
  download-link rules, only-own-photos rule, approval mode, multiple drives,
  offline drive, Drive links scanned in place with a stand-in for Google
  (nothing written to disk, preview, download, zip, deletion, resume), admin
  selfie test, VLAN lists, certificate, busy-port fallback.
- Tested by running the real app (12 November, Linux build machine): HTTPS
  start with the usual port busy (it moved to the next port), real face
  models on 8 sample photos from 3 folders, admin test and full guest flow in
  a browser at phone size (390 px) and laptop size (1366 px). The sample was
  one person in several versions of the same picture, so it shows that the
  engine works, not how accurate it is on a real wedding album.
- Not tested (not possible on the build machine): real Google Drive access,
  real mail sending, Windows-only steps (browser auto-open, certificate
  trust), a real phone camera. The Windows and macOS test runs happen on
  GitHub after the push.

## Open items

1. Get OTP mail working on the office network (app password or Brevo).
2. First real scan and selfie match on a small folder, to check accuracy.
3. Connect Google Drive with a key file from a personal Google account, then
   check on one real Drive folder that the scan reads photos without copying.
4. Optional later: "Sign in with Google" instead of the key file, QR code for
   the guest link, hosting on a domain.
