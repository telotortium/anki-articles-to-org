import argparse
import copy
import json
import logging
import os
import os.path
import random
import requests
import shutil
import stat
import subprocess
import sys
import time
import threading

import importlib.machinery
import importlib.util

from itertools import islice

# Create logger that logs to standard error
logger = logging.getLogger("anki-articles-to-org")
# These 2 lines prevent duplicate log lines.
logger.handlers.clear()
logger.propagate = False
level = os.environ.get("ANKI_ARTICLES_TO_ORG_LOGLEVEL", logging.INFO)
logger.setLevel(level)

# Create handler that logs to standard error
handler = logging.StreamHandler()
handler.setLevel(level)

# Create formatter and add it to the handler
formatter = logging.Formatter("[%(levelname)8s %(asctime)s - %(name)s] %(message)s")
handler.setFormatter(formatter)

# Add handler to the logger
logger.addHandler(handler)

ANKI_SUSPENDED_TAG = "anki:suspend"
FAVORITE_TAG = "marked"
anki_url = "http://localhost:8765"
version = 6


def batched(iterable, n):
    "Batch data into tuples of length n. The last batch may be shorter."
    # batched('ABCDEFG', 3) --> ABC DEF G
    if n < 1:
        raise ValueError("n must be at least one")
    it = iter(iterable)
    while batch := tuple(islice(it, n)):
        yield batch


def ankiconnect_request(payload):
    logger.debug("payload = %s", payload)
    response = json.loads(requests.post(anki_url, json=payload).text)
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
                "version": version,
                "params": {
                    "cards": cards,
                },
            }
        )
        mod_time = min(x["mod"] for x in response["result"])
        ankiconnect_request(
            {
                "action": "updateNoteFields",
                "version": version,
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
    # chr is needed because f-strings don't support backslash or
    # pound sign in the string.
    # chr(35) = '#'
    # chr(10) = '\n'
    content = (
        f"""\
{chr(35)}+setupfile: common.setup
{chr(35)}+date: [{time_added_org_date}]

* {ni['fields']['given_title']['value']}
:PROPERTIES:
:ID: anki_article_{note_id}
:ROAM_REFS: {ni['fields']['given_url']['value']}
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
    content_changed = False
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
        try:
            mode = stat.S_IMODE(os.stat(output_file).st_mode)
            mode |= stat.S_IRUSR | stat.S_IWUSR
            os.chmod(output_file, mode)
        except FileNotFoundError:
            pass
        with open(output_file, "wb") as f:
            f.write(content_encoded)
        # Remove write permission to prevent Emacs from updating the
        # files by accident.
        mode = stat.S_IMODE(os.stat(output_file).st_mode)
        logger.debug(f"{note_id}: begin mode = {stat.filemode(mode)}")
        mode &= (~stat.S_IWUSR) & (~stat.S_IWGRP) & (~stat.S_IWOTH)
        logger.debug(f"{note_id}: end mode = {stat.filemode(mode)}")
        os.chmod(output_file, mode)


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
    parser = argparse.ArgumentParser(
        prog="anki-articles-to-org",
        description="Export Article notes in Anki as individual Org-mode files to a directory.",
    )
    parser.add_argument("output_dir", help="The directory to export article notes to.")
    args = parser.parse_args()

    # First, find notes added to Anki but not yet to Pocket and add them to
    # Pocket.
    deck_name = "Articles"
    note_type = "Pocket Article"
    response = ankiconnect_request(
        {
            "action": "findNotes",
            "version": version,
            "params": {
                # Find notes with `given_url` and `given_title` not empty, but
                # `item_id` empty.
                "query": f'"note:{note_type}" "deck:{deck_name}"'
            },
        }
    )
    note_ids = response["result"]
    response = ankiconnect_request(
        {
            "action": "notesInfo",
            "version": version,
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


if __name__ == "__main__":
    main()
