"""One door for every place photos can live: this computer / a network drive
(plain folders), Google Drive, Amazon S3 (and compatible) and Dropbox.

A folder on an album is stored as text:  C:\\Photos | gdrive:ID | s3://bucket/prefix/ | dropbox:/Folder
A photo that lives in a remote place also has a `ref` that says where it is:
a Drive id, 's3:bucket/key' or 'dbx:id:...'.
"""
import gdrive
import dropbox_src
import s3src

Error = gdrive.DriveError          # S3 and Dropbox errors are kinds of this


def is_remote(root):
    return root.startswith(("gdrive:", "s3://", "dropbox:"))


def kind(root):
    return ("drive" if root.startswith("gdrive:") else "s3" if root.startswith("s3://")
            else "dropbox" if root.startswith("dropbox:") else "local")


def ref_kind(ref):
    return "s3" if ref.startswith("s3:") else "dropbox" if ref.startswith("dbx:") else "drive"


def label(root):
    k = kind(root)
    return (gdrive.label(root) if k == "drive" else s3src.label(root) if k == "s3"
            else dropbox_src.label(root) if k == "dropbox" else root)


def parse_link(text):
    """Text typed by the admin -> ('s3'|'dropbox'|'drive', entry) or None for a local folder."""
    e = s3src.parse_link(text)
    if e:
        return "s3", e
    e = dropbox_src.parse_link(text)
    if e is not None:
        return "dropbox", e
    fid = gdrive.parse_link(text)
    if fid:
        return "drive", "gdrive:" + fid
    return None


def connected(k):
    return {"drive": lambda: bool(gdrive.key_info()), "s3": s3src.connected,
            "dropbox": dropbox_src.connected}[k]()


def walk(root):
    k = kind(root)
    if k == "s3":
        yield from s3src.walk(root)
    elif k == "dropbox":
        dropbox_src.check(root)
        yield from dropbox_src.walk(root)
    else:
        fid = root[7:]
        gdrive.remember_name(fid, gdrive.folder_name(fid))
        yield from gdrive.walk(fid)


def stamp(info):
    k = info.get("src")
    return (s3src.stamp(info) if k == "s3" else dropbox_src.stamp(info) if k == "dropbox"
            else gdrive.stamp(info))


def scan_image_bytes(info):
    """(bytes, extension, 'small'|'full') of a remote photo for scanning, or None
    to let the caller use the Drive path."""
    k = info.get("src")
    if k == "dropbox":
        data = dropbox_src.fetch_small(info, 2048)
        if data:
            return data, ".jpg", "small"
        return dropbox_src.fetch_full(info["id"]), "." + info["name"].rsplit(".", 1)[-1], "full"
    if k == "s3":
        return s3src.fetch_full(info["id"]), "." + info["name"].rsplit(".", 1)[-1], "full"
    return None


def fetch_small(ref, side, name=""):
    k = ref_kind(ref)
    if k == "dropbox":
        return dropbox_src.fetch_small({"id": ref, "name": name}, side)
    if k == "s3":
        return None                       # S3 has no ready-made small copy
    return gdrive.fetch_small({"id": ref}, side)


def fetch_full(ref):
    k = ref_kind(ref)
    return (s3src.fetch_full(ref) if k == "s3" else dropbox_src.fetch_full(ref) if k == "dropbox"
            else gdrive.fetch_full(ref))


def open_original(ref):
    k = ref_kind(ref)
    return (s3src.open_original(ref) if k == "s3" else dropbox_src.open_original(ref)
            if k == "dropbox" else gdrive.open_original(ref))
