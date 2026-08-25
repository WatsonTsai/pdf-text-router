#!/usr/bin/env python3
"""Download the public PDFs listed in corpus.json.

The README's numbers come from these files. Nothing is vendored into the
repository -- several of the sources allow redistribution and several do not,
so the repo carries URLs and checksums and you fetch your own copy:

  python scripts/fetch_corpus.py                # into ./corpus
  python scripts/fetch_corpus.py --dest /tmp/x  # somewhere else
  python scripts/fetch_corpus.py --record       # write sha256 back into the
                                                # manifest (maintainer only)

Files already present with a matching sha256 are left alone, so re-running is
cheap. A source that has moved is reported and skipped rather than aborting
the run: a benchmark you cannot reproduce at all is worse than one that is
short a file, but you should know which one is missing.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "corpus.json")
UA = "pdf-text-router-benchmark/0.1 (+https://github.com/WatsonTsai/pdf-text-router)"
DELAY = 1.0     # be a polite guest on other people's servers


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def local_name(entry):
    return entry["id"].replace("/", "__") + ".pdf"


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    if not data.startswith(b"%PDF"):
        raise ValueError("not a PDF (got %r)" % data[:16])
    tmp = dest + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)
    return len(data)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", default=os.path.join(HERE, "..", "corpus"))
    ap.add_argument("--record", action="store_true",
                    help="write the sha256 of each file back into corpus.json")
    ap.add_argument("--only", help="substring filter on the entry id")
    args = ap.parse_args()

    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    entries = manifest["files"]
    if args.only:
        entries = [e for e in entries if args.only in e["id"]]
    dest_dir = os.path.abspath(args.dest)
    os.makedirs(dest_dir, exist_ok=True)

    got = skipped = failed = 0
    total_bytes = 0
    for entry in entries:
        path = os.path.join(dest_dir, local_name(entry))
        want = entry.get("sha256")
        if os.path.isfile(path) and (not want or sha256(path) == want):
            skipped += 1
            total_bytes += os.path.getsize(path)
            continue
        try:
            size = fetch(entry["url"], path)
        except (urllib.error.URLError, ValueError, OSError) as exc:
            print("  FAILED  %-44s %s" % (entry["id"][:44], str(exc)[:50]))
            failed += 1
            continue
        digest = sha256(path)
        if want and digest != want:
            print("  CHANGED %-44s upstream file no longer matches manifest"
                  % entry["id"][:44])
        entry["sha256"] = digest if args.record else entry.get("sha256", digest)
        got += 1
        total_bytes += size
        print("  ok      %-44s %8d bytes" % (entry["id"][:44], size))
        time.sleep(DELAY)

    print("\n%d downloaded, %d already present, %d failed, %.1f MB total"
          % (got, skipped, failed, total_bytes / 1e6))
    print("corpus at %s" % dest_dir)

    if args.record:
        with open(MANIFEST, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
        print("recorded checksums into corpus.json")
    return 1 if failed and not got else 0


if __name__ == "__main__":
    sys.exit(main())
