# For docs, see ../setup.py
import argparse
import json
import logging
import os
import os.path
import pdb
import random
import re
import shutil
import subprocess
import sys
import time
import threading
import traceback

from itertools import islice

import requests

# Create logger that logs to standard error
logger = logging.getLogger("anki-articles-to-org")
# These 2 lines prevent duplicate log lines.
logger.handlers.clear()
logger.propagate = False

LEVEL_DEFAULT = logging.INFO
level = os.environ.get("ANKI_ARTICLES_TO_ORG_LOGLEVEL")
if level:
    level = level.upper()
else:
    level = LEVEL_DEFAULT
logger.setLevel(level)

# Create handler that logs to standard error
handler = logging.StreamHandler()
handler.setLevel(level)

# Create formatter and add it to the handler
formatter = logging.Formatter("[%(levelname)8s %(asctime)s - %(name)s] %(message)s")
handler.setFormatter(formatter)

# Add handler to the logger
logger.addHandler(handler)

ANKICONNECT_URL_DEFAULT = "http://localhost:8765"
ankiconnect_url = os.environ.get(
    "ANKI_ARTICLES_TO_ORG_ANKICONNECT_URL", ANKICONNECT_URL_DEFAULT
)
ANKICONNECT_VERSION = 6


def batched(iterable, n):
    "Batch data into tuples of length n. The last batch may be shorter."
    # batched('ABCDEFG', 3) --> ABC DEF G
    if n < 1:
        raise ValueError("n must be at least one")
    it = iter(iterable)
    while batch := tuple(islice(it, n)):
        yield batch


def ankiconnect_request(payload):
    payload["version"] = ANKICONNECT_VERSION
    logger.debug("payload = %s", payload)
    response = json.loads(requests.post(ankiconnect_url, json=payload, timeout=3).text)
    logger.debug("response = %s", response)
    if response["error"] is not None:
        logger.warning("payload %s had response error: %s", payload, response)
    return response


BATCH_SIZE = 50


def pocket_batch(collection, f_per_item, f_commit):
    if collection:
        for batch in batched(collection, BATCH_SIZE):
            for x in batch:
                f_per_item(x)
            f_commit()


def html_to_org(html):
    proc = subprocess.Popen(
        [shutil.which("pandoc"), "-fhtml", "-torg"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    (org, _) = proc.communicate(input=html.encode("utf-8"), timeout=5)
    if proc.returncode != 0:
        raise Exception(f"pandoc failed - returncode = {proc.returncode}")
    return org.decode(encoding="utf-8", errors="strict")


empty_title_notes = []


def log_empty_title(note_id):
    logger.warn(f"{note_id}: title is empty - please fix!")
    empty_title_notes.append(note_id)


def write_org_file(index, total, output_dir, ni):
    note_id = ni["noteId"]
    output_file = os.path.join(output_dir, f"{note_id}.org")
    logger.info(f"{index}/{total} {note_id}: output_file = {output_file}")
    time_added_str = ni["fields"]["time_added"]["value"].strip()
    # Handle time_added missing by setting it from card mod time
    if time_added_str:
        time_added = int(time_added_str)
    else:
        cards = ni["cards"]
        response = ankiconnect_request(
            {
                "action": "cardsModTime",
                "params": {
                    "cards": cards,
                },
            }
        )
        mod_time = min(x["mod"] for x in response["result"])
        ankiconnect_request(
            {
                "action": "updateNoteFields",
                "params": {
                    "note": {
                        "id": note_id,
                        "fields": {
                            "time_added": str(mod_time),
                        },
                    },
                },
            }
        )
        time_added = int(mod_time)
    time_added_struct = time.localtime(time_added)
    time_added_org_date = time.strftime("%Y-%m-%d %a %H:%M", time_added_struct)
    time_added_ymd = time.strftime("%Y-%m-%d", time_added_struct)
    personal_notes = html_to_org(ni["fields"]["personal_notes"]["value"]).strip()
    summary = html_to_org(ni["fields"]["summary"]["value"]).strip()
    excerpt = html_to_org(ni["fields"]["excerpt"]["value"]).strip()

    def retrieve_and_fixup_url(note_info, url_field):
        link = note_info["fields"][url_field]["value"].strip()
        if not link:
            return None
        match = re.match(r'<a href="?(.*?)"?>(.*)</a>', link)
        if not match:
            return link
        return f"[[{match[1]}][{match[2]}]]"

    primary_url = retrieve_and_fixup_url(ni, "given_url")
    alternate_url = retrieve_and_fixup_url(ni, "resolved_url")
    if not primary_url:
        primary_url = alternate_url
        alternate_url = None
    if primary_url == alternate_url:
        alternate_url = None

    primary_title = ni["fields"]["given_title"]["value"].strip()
    alternate_title = ni["fields"]["resolved_title"]["value"].strip()
    if not primary_title:
        primary_title = alternate_title
        alternate_title = None
    if primary_title == alternate_title:
        alternate_title = None

    if not primary_title:
        log_empty_title(note_id)
        primary_title = primary_url

    # chr is needed because f-strings don't support backslash or
    # pound sign in the string.
    # chr(35) = '#'
    # chr(10) = '\n'
    content = (
        f"""\
{chr(35)}+setupfile: common.setup
{chr(35)}+date: [{time_added_org_date}]
{chr(35)}+comment: DO NOT EDIT - run ~anki-articles-to-org~ to re-export from Anki

* {primary_title}
:PROPERTIES:
:ID: anki_article_{note_id}
:ROAM_REFS: {primary_url}{" " + alternate_url if alternate_url else ""}{(chr(10) + ':ROAM_ALIASES: "' + alternate_title + '"') if alternate_title else ''}
:END:

{"** Personal Notes" + chr(10) + chr(10) + personal_notes + chr(10) + chr(10) if personal_notes else ""}"""
        + f"""{"** Summary" + chr(10) + chr(10) + summary + chr(10) + chr(10) if summary else ""}"""
        + f"""{"** Excerpt" + chr(10) + chr(10) + excerpt + chr(10) + chr(10) if excerpt else ""}"""
        + f"""[[roam:{time_added_ymd}][{time_added_ymd}]]
"""
    )
    logger.debug(f"{note_id}: content =\n{content}")
    content_encoded = content.encode("utf-8")

    # Only update file if content changed, to preserve modtime.
    old_content = None
    try:
        with open(output_file, "rb") as f:
            old_content = f.read()
    except FileNotFoundError:
        pass
    if old_content == content_encoded:
        logger.info(f"{note_id}: content unchanged - not updating {output_file}")
    else:
        logger.info(f"{note_id}: content changed - updating {output_file}")
        logger.debug(
            f"{note_id}: old_content =\n{old_content.decode(encoding='utf-8', errors='strict')}"
        )
        with open(output_file, "wb") as f:
            f.write(content_encoded)


def schedule_thread(threads, index, total, output_dir, ni):
    threads_done = True
    for i in range(len(threads)):
        thread = threads[i]
        if thread:
            thread.join()
            if thread.is_alive():
                threads_done = False
            else:
                threads[i] = None
        if ni and threads[i] is None:
            thread = threading.Thread(
                target=write_org_file, args=(index, total, output_dir, ni)
            )
            threads[i] = thread
            thread.start()
            threads_done = False
            break
    return not threads_done


def main():
    try:
        _main()
    except Exception:
        debug = os.environ.get("ANKI_ARTICLES_TO_ORG_DEBUG", None)
        if debug and debug != "0":
            _extype, _value, tb = sys.exc_info()
            traceback.print_exc()
            pdb.post_mortem(tb)
        else:
            raise


def _main():
    parser = argparse.ArgumentParser(
        prog="anki-articles-to-org",
        description="Export Article notes in Anki as individual Org-mode files to a directory.",
        epilog=f"""Environment variables:

- ANKI_ARTICLES_TO_ORG_ANKICONNECT_URL: set to the URL of AnkiConnect. Default:
  {ANKICONNECT_URL_DEFAULT}
  set to "{ANKICONNECT_URL_DEFAULT}".
- ANKI_ARTICLES_TO_ORG_DEBUG: set in order to debug using PDB upon exception.
- ANKI_ARTICLES_TO_ORG_LOGLEVEL: set log level. Default: {LEVEL_DEFAULT}
""",
    )
    parser.add_argument("output_dir", help="The directory to export article notes to.")
    parser.add_argument(
        "--edited", type=int, help="Only examine notes modified in the past N days."
    )
    args = parser.parse_args()

    # First, find notes added to Anki but not yet to Pocket and add them to
    # Pocket.
    deck_name = "Articles"
    note_type = "Pocket Article"
    response = ankiconnect_request(
        {
            "action": "findNotes",
            "params": {
                # Find notes with `given_url` and `given_title` not empty, but
                # `item_id` empty.
                "query": f'"note:{note_type}" "deck:{deck_name}"'
                + (f" edited:{args.edited}" if args.edited else ""),
            },
        }
    )
    note_ids = response["result"]
    response = ankiconnect_request(
        {
            "action": "notesInfo",
            "params": {
                "notes": note_ids,
            },
        }
    )
    note_infos = response["result"]
    if note_infos:
        random.shuffle(note_infos)
        threads = [None] * BATCH_SIZE
        for i, ni in enumerate(note_infos):
            schedule_thread(threads, i, len(note_infos), args.output_dir, ni)
        while schedule_thread(threads, None, None, None, None):
            pass
    if empty_title_notes:
        logger.warn(f"Note IDs with no title: {empty_title_notes}")


if __name__ == "__main__":
    main()
