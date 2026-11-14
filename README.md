# FaceFind – selfie in, only your photos out

A guest opens your link, proves their email with a one-time code sent from
**your** mail id, takes a selfie, and sees only the photos they are in.
Nobody else can open or download them.

## Start it (Windows)

1. Install Python 3.11 or newer from python.org (tick **Add Python to PATH**).
2. Double-click **start.bat**. The first run installs what is needed and
   downloads the face models (about 40 MB).
3. Answer the setup questions in the window: studio name, admin password
   (6 characters or more), the mail id that sends the OTP, and the port.
   To change the answers later run `setup.bat`.
4. The window prints the addresses, for example
   `https://192.168.1.50:8443/admin`. Open it and sign in with your admin
   password.

On Mac or Linux use `./start.sh` instead.

## The `.env` settings

The setup questions write this file for you; you can also edit it by hand.

| Setting | What to put |
|---|---|
| `BRAND` | Your studio name (shown on pages and on the preview watermark) |
| `ADMIN_PASSWORD` | A long password only you know |
| `SMTP_USER`, `MAIL_FROM` | The mail id that sends the OTP (personal or official) |
| `SMTP_PASSWORD` | Gmail: an **App password** (Google Account → Security → 2-Step Verification → App passwords). Not your normal password |
| `DEV_MODE` | `1` only while trying it on your own computer; `0` for real guests |
| `TRUST_PROXY` | `1` once you put it online behind HTTPS (next section) |

## One link for all events

Give every album a **Date of the event** (asked when you create it; change it
later under "Change the event date"). The admin page then shows **One link
for all events**. A guest who opens it signs in once with email and code,
chooses the dates of the events they attended, takes one selfie, and gets
their photos from every open album inside those dates, grouped by event.
Albums without a date are reachable only through their own link. Set
`PORTAL=0` in `.env` to switch the general link off.

## Guest accounts, saved photos, download record

Guests sign in with name, phone and email (a code is sent to the email).
Under every photo there is **Save**; saved photos are kept for that email
and open at the next sign-in without a new selfie. The admin page lists
**Who downloaded photos** (name, phone, email, which photos) and offers the
full record as a file for Excel. **Match strictness** on the admin page sets
how alike two faces must be; use "Test with my own selfie" to see the scores.

## Admin sections

- **Albums** - add albums, search by name or date, 20 per page; each album
  opens to show its link, scan status, files that were not scanned, the
  selfie test and its folders.
- **Guests and downloads** - who signed in and which photos they took;
  search by name, phone or email; full record for Excel.
- **Look and promotions** - studio name, colour, logo, sign-in background,
  logo on downloaded photos, next events and offers (before / after sign-in).
- **Staff** - user names and passwords for helpers. Staff can add and scan
  albums only. The owner signs in with the `.env` password and an empty
  user name.
- **Settings** - Google Drive, match strictness, scan detail, activity log.

## On the web

The app keeps one port: the first start chooses it (8443, or the next free
one) and writes it to `data/port.txt`; every later start uses the same
port. If that port is taken, the app says so and stops - usually FaceFind
is already running in another window.

Double-click `start-web.bat`. It downloads Cloudflare's tunnel program,
starts the app and prints a public address ending in `trycloudflare.com`.
The office-network address keeps working at the same time.
Guests open that address; the admin is that address plus `/admin`. No router
changes are needed and HTTPS is included. Keep the window open; the PC must
stay on, and the address changes at every start.

For a fixed address on your own domain (the domain must be on a free
Cloudflare account), run these once in the facefind folder, then use the
last line instead of `start-web.bat`:

    cloudflared.exe tunnel login
    cloudflared.exe tunnel create facefind
    cloudflared.exe tunnel route dns facefind photos.yourdomain.com
    cloudflared.exe tunnel run --url https://127.0.0.1:PORT --no-tls-verify facefind
    (the app's one port answers http:// and https://, by 127.0.0.1 and by localhost,
    so a tunnel origin of http://127.0.0.1:PORT or https://localhost:PORT works too;
    the start-up window prints a "Web check" with the addresses that work)

(PORT is the number in `data/port.txt`) and put `TRUST_PROXY=1` and
`PUBLIC_URL=https://photos.yourdomain.com` in `.env`.
Once it is on the web, use a long admin password.

## Photos from many drives

When you create an event, type one folder per line – from as many drives as
you like:

```
D:\Photos\Sharma wedding
E:\Backup\Sharma wedding
\\NAS\events\Sharma
```

- **Hard disks, external disks, NAS:** use the path as it is.
- **Google Drive:** paste the folder **link** (see "Google Drive links"
  below). Do not use the `G:` drive letter of *Google Drive for desktop*:
  through `G:` Windows downloads every photo before the app can read it.
- **OneDrive / Dropbox:** install their desktop app; use the synced folder.
- **Anything else (S3, many Google accounts, etc.):** `rclone mount` turns it
  into a drive letter.

Press **Scan photos**. The scan reads every photo once and stores a face
signature (numbers, not the picture). Press it again any time to pick up new
photos – already-scanned photos are skipped. Use **Add / change drives** on an
event to add more folders later.

If a drive is switched off, its photos stay in the index and work again when
it is back. Photos never leave your drives; originals are read only at the
moment a verified guest downloads them.

## Google Drive links

You can paste a Google Drive **folder link** instead of choosing a folder.
One-time setup (the admin page shows the same steps):

1. Open console.cloud.google.com and create a project.
2. APIs and Services → Library → **Google Drive API** → Enable.
3. IAM and Admin → Service Accounts → Create service account → Done.
4. Open the account → Keys → Add key → Create new key → JSON.
5. Upload the downloaded `.json` file under **Google Drive links** on the
   admin page.

The page then shows a "robot" email. Share each Drive folder with that email
(Viewer is enough) – this works for your own folders and for folders where you
are an editor. Folders set to "Anyone with the link" need no sharing.

**Drive photos are scanned in place - nothing is copied to this computer.**
For each photo the app asks Google for a ready-made 1600-pixel version
(about 0.3 MB instead of the 5-25 MB original), reads it into memory, stores
the face signatures and throws the picture away. Previews and downloads are
passed straight from Drive to the guest when they ask. "Scan again" reads
only new or changed photos and removes photos deleted on Drive. A copy made
by an older version in `data/drive` is deleted after the next scan. Keep the key file private; it is never sent
to GitHub.

## Using it on your LAN and VLANs

The app listens on every network card of the computer and serves **HTTPS**
by itself. Settings are in `.env`:

| Setting | Meaning |
|---|---|
| `PORT` | First port to try (8443). If it is busy, the app takes the next free port and prints it |
| `HOST` | `0.0.0.0` = whole LAN, `127.0.0.1` = this computer only |
| `HTTPS` | `1` = built-in HTTPS with an automatic certificate |
| `ALLOWED_NETWORKS` | Networks / VLANs allowed to open the app, e.g. `192.168.10.0/24,192.168.20.0/24`. Empty = any |
| `ADMIN_NETWORKS` | Networks allowed to open `/admin`, e.g. only your office VLAN |

Three things to do once:

1. **Windows Firewall:** allow the port. In an administrator Command Prompt:
   `netsh advfirewall firewall add rule name="FaceFind" dir=in action=allow protocol=TCP localport=8443`
   (use the port the window printed).
2. **VLANs:** if guests are on a different VLAN than this computer, add a rule
   on your router / L3 switch / firewall that lets the guest VLAN reach this
   computer's IP on that one TCP port. Nothing else needs to be opened.
3. **Give this computer a fixed IP** (DHCP reservation), so the guest link and
   QR code keep working.

**About the browser warning:** the LAN certificate is made by the app itself,
so each device shows "connection is not private" the first time. Press
*Advanced → Proceed*; the connection is still encrypted and the camera works.
To remove the warning on your own office devices, install `data/tls/cert.pem`
as a trusted certificate. For guests, the warning goes away completely once
you move to a domain (below).

## Hosting on a domain later

1. Point the domain to the server and put Caddy, Nginx or a Cloudflare Tunnel
   in front; it provides a real HTTPS certificate.
2. In `.env` set `HTTPS=0`, `TRUST_PROXY=1`, `HOST=127.0.0.1`,
   `PUBLIC_URL=https://photos.yourdomain.com`, and `DEV_MODE=0`.
3. Restart. Guest links on the admin page now use the domain.

Never expose the app to the internet over plain `http://`.

## What protects the photos

- Email must pass a 6-digit OTP: valid 5 minutes, single use, locked after 5
  wrong tries, at most 3 codes per email per 15 minutes.
- The verified session lives in a browser cookie scripts cannot read; it ends
  after 2 hours.
- A guest can open only photos their own selfie matched. Everything else
  answers "not found".
- Previews are small and watermarked. Originals go through a link that expires
  in 2 minutes and works only in the verified browser.
- **Approval mode** (on by default): you see the selfie and email and press
  Approve before any photo is released.
- Selfies are not stored (in approval mode: only until you decide).
- Every code, match and download is written to the activity log.
- Guests can press "Sign out and erase" to delete their matches.

## Limits you should know

- **Approval mode is your protection against someone holding up another
  person's picture as the "selfie".** There is no automatic liveness check in
  this version. Keep approval on for private events.
- Face matching can be wrong sometimes. Raise `MATCH_THRESHOLD` (e.g. 0.45) for
  fewer wrong photos; lower it (0.36) if guests miss photos.
- This version suits one computer and events up to a few lakh photos each.
  Scanning runs on the CPU at roughly a few photos per second. Crores of
  photos need the bigger setup (GPU scanning + a vector database such as
  Milvus); `faces.py` and `indexer.py` are the two files to swap.
- Photo types read: JPG, PNG, WEBP, BMP, TIFF, HEIC (needs `pillow-heif`, installed by
  `start.bat`) and camera RAW files (ARW, CR2, CR3, NEF, DNG, RAF, RW2, ORF ...).
- Face signatures are biometric personal data. Take consent (the guest page
  asks for it), tell people at the event, and close the event link when the
  job is done.

## Check it yourself

`python tests/test_flow.py` runs the security rules end to end (with a
stand-in face engine, so it needs no models). `python tests/test_engine.py`
checks that the real face models load. GitHub runs both on Windows, Linux
and macOS after every push (`.github/workflows/ci.yml`).
