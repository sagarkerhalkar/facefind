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

## Photos from many drives

When you create an event, type one folder per line – from as many drives as
you like:

```
D:\Photos\Sharma wedding
E:\Backup\Sharma wedding
G:\My Drive\Sharma wedding
\\NAS\events\Sharma
```

- **Hard disks, external disks, NAS:** use the path as it is.
- **Google Drive:** install *Google Drive for desktop*; it appears as a drive
  letter (usually `G:`).
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
- HEIC and RAW files are skipped; JPG, PNG, WEBP and BMP are read.
- Face signatures are biometric personal data. Take consent (the guest page
  asks for it), tell people at the event, and close the event link when the
  job is done.

## Check it yourself

`python tests/test_flow.py` runs the security rules end to end (with a
stand-in face engine, so it needs no models).
