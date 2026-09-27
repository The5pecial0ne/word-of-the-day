#!/usr/bin/env python3
"""
wotd - a tiny Finnish word-of-the-day for your Mac.

Everything lives in one SQLite file next to this script. No pip installs,
no servers. A tiny helper (wotd-listener) waits for macOS to say "the
screen just woke up" or "someone just unlocked", then runs `wotd.py show`.

Commands you'll actually type:
    python3 wotd.py add talo "house"     add your own word
    python3 wotd.py today                print today's word in the terminal
    python3 wotd.py import 30            pull 30 new words from the web now
    python3 wotd.py stats                how many words, how many seen
    python3 wotd.py show                 pop up today's word right now
"""

import datetime
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.parse

# ---------------------------------------------------------------------------
# Settings. Tweak these if you like; nothing else needs touching.
# ---------------------------------------------------------------------------

HOME = os.environ.get("WOTD_HOME", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(HOME, "words.db")
WORDLIST_PATH = os.path.join(HOME, "fi_50k.txt")

# The most common 50,000 Finnish words, ranked by how often they show up
# in subtitles. We walk down this list from the top, so you learn the
# useful stuff first.
WORDLIST_URL = (
    "https://raw.githubusercontent.com/hermitdave/FrequencyWords/"
    "master/content/2018/fi/fi_50k.txt"
)
# Wiktionary gives English meanings for Finnish words, free and without a key.
DEFINITION_URL = "https://en.wiktionary.org/api/rest_v1/page/definition/{}"
USER_AGENT = "wotd/1.0 (personal Finnish vocabulary tool)"

LOW_WATER_MARK = 15      # when fewer unseen words than this remain...
TOP_UP_BATCH = 30        # ...go and fetch this many more
RETRY_AFTER = 60 * 60    # if the web was unreachable, wait an hour before retrying

DIALOG_TITLE = "Finnish word of the day"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
    id        INTEGER PRIMARY KEY,
    word      TEXT NOT NULL UNIQUE,   -- UNIQUE is our duplicate guard
    meaning   TEXT NOT NULL,
    source    TEXT NOT NULL,          -- 'manual' or 'web'
    rank      INTEGER,                -- position in the frequency list, if any
    added_at  TEXT NOT NULL,
    shown_on  TEXT                    -- the day it was word of the day
);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect():
    os.makedirs(HOME, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    clean_up_old_meanings(db)
    return db


def clean_up_old_meanings(db):
    """One-time repair for words saved before the CSS fix. Runs once, then never again."""
    if get_state(db, "meanings_tidied") == "1":
        return
    for word_id, meaning in db.execute("SELECT id, meaning FROM words").fetchall():
        cleaned = tidy(meaning)
        if cleaned and cleaned != meaning:
            db.execute("UPDATE words SET meaning = ? WHERE id = ?", (cleaned, word_id))
    set_state(db, "meanings_tidied", "1")  # also commits


def get_state(db, key, default=None):
    row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(db, key, value):
    db.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    db.commit()


def normalize(word):
    """Make 'Talo ', 'talo' and 'TALO' count as the same word.

    NFC matters more than it looks: 'ä' can be stored either as one
    character or as a plain 'a' followed by the two dots as a separate
    mark. Without this, the two versions would sneak past each other as
    different words.
    """
    return unicodedata.normalize("NFC", word).strip().lower()


def add_word(db, word, meaning, source, rank=None):
    """Insert a word. Returns False if it was already there."""
    cur = db.execute(
        "INSERT OR IGNORE INTO words (word, meaning, source, rank, added_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (normalize(word), meaning.strip(), source, rank, now().isoformat(timespec="seconds")),
    )
    db.commit()
    return cur.rowcount == 1


def unseen_count(db):
    return db.execute("SELECT COUNT(*) FROM words WHERE shown_on IS NULL").fetchone()[0]


# ---------------------------------------------------------------------------
# Picking today's word
# ---------------------------------------------------------------------------

def todays_word(db):
    """Same word all day. A fresh one the first time we're asked on a new day."""
    today = now().date().isoformat()

    row = db.execute(
        "SELECT word, meaning FROM words WHERE shown_on = ?", (today,)
    ).fetchone()
    if row:
        return row

    # Your own words jump the queue, then we go down the frequency list.
    row = db.execute(
        "SELECT id, word, meaning FROM words WHERE shown_on IS NULL "
        "ORDER BY source = 'web', rank, id LIMIT 1"
    ).fetchone()

    if row is None:
        # Seen everything? Start a new lap, oldest-seen first.
        row = db.execute(
            "SELECT id, word, meaning FROM words ORDER BY shown_on, id LIMIT 1"
        ).fetchone()
        if row is None:
            return None  # the list is completely empty

    db.execute("UPDATE words SET shown_on = ? WHERE id = ?", (today, row[0]))
    db.commit()
    return row[1], row[2]


# ---------------------------------------------------------------------------
# The web side: frequency list + Wiktionary
# ---------------------------------------------------------------------------

class Offline(Exception):
    """The internet isn't there right now (just woke up, no Wi-Fi yet, etc.)."""


def fetch(url, timeout=10):
    """Download a URL with the curl that ships with macOS.

    Using curl instead of Python's urllib dodges the classic "SSL certificate
    verify failed" problem some Python installs have on Macs.

    Returns the text, or None if the server said "not found". Raises Offline
    if we couldn't reach the server at all, because that's a different
    situation: the word is fine, we just have to try again later.
    """
    result = subprocess.run(
        ["curl", "-fsSL", "--max-time", str(timeout), "-A", USER_AGENT, url],
        capture_output=True,
    )
    if result.returncode == 22:  # curl's code for "the server answered with an error"
        return None
    if result.returncode != 0:
        raise Offline()
    return result.stdout.decode("utf-8", errors="replace")


def load_wordlist():
    """The frequency list, downloaded once and kept next to the database."""
    if not os.path.exists(WORDLIST_PATH):
        text = fetch(WORDLIST_URL, timeout=60)
        if not text:
            return None
        with open(WORDLIST_PATH, "w", encoding="utf-8") as f:
            f.write(text)
    with open(WORDLIST_PATH, encoding="utf-8") as f:
        # Each line looks like "talo 12345"; we only want the word.
        return [line.split()[0] for line in f if line.strip()]


def looks_like_a_real_word(word):
    # Skip numbers, single letters and anything with odd characters.
    return len(word) >= 2 and re.fullmatch(r"[a-zåäö-]+", word) is not None


# Wiktionary entries like "genitive singular of talo" are just grammar forms.
# We'd rather learn "talo" itself, so those get skipped.
FORM_OF = re.compile(
    r"\b(inflection|form|singular|plural|case|participle|infinitive|person|"
    r"comparative|superlative|possessive|clipping|abbreviation|misspelling)\b"
    r"[^.;]*\bof\b",
    re.IGNORECASE,
)
SKIP_PARTS_OF_SPEECH = {"proper noun", "letter", "symbol", "suffix", "prefix"}


def strip_html(fragment):
    # Wiktionary sometimes tucks a <style> block inside a definition. Its
    # contents are CSS, not words, so drop the whole block before the tags.
    fragment = re.sub(
        r"<(style|script)\b[^>]*>.*?</\1>", "", fragment, flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(r"<[^>]+>", "", fragment)
    return tidy(html.unescape(text))


def tidy(text):
    """Polish a meaning so it reads nicely in the popup."""
    # Leftover CSS rules like ".mw-parser-output .deprecated{color:...}".
    text = re.sub(r"(?:[.#][\w-]+[\s,>]*)+\{[^{}]*\}", "", text)
    # Grammar labels such as "[with connegative]" are dictionary jargon.
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,;.])", r"\1", text)  # no stray space before punctuation
    return text.strip(" ,;")


def meaning_from_wiktionary(word):
    """A short English meaning for a Finnish word, or None if there isn't a good one."""
    raw = fetch(DEFINITION_URL.format(urllib.parse.quote(word)))
    if not raw:
        return None
    try:
        entries = json.loads(raw).get("fi", [])
    except ValueError:
        return None

    meanings = []
    for entry in entries:
        if entry.get("partOfSpeech", "").lower() in SKIP_PARTS_OF_SPEECH:
            continue
        for d in entry.get("definitions", []):
            definition_html = d.get("definition", "")
            if "form-of" in definition_html:
                continue
            text = strip_html(definition_html)
            if not text or FORM_OF.search(text):
                continue
            # Trim things like "(transitive) " so the popup stays clean.
            text = re.sub(r"^\([^)]*\)\s*", "", text)
            if text and text not in meanings:
                meanings.append(text)
    if not meanings:
        return None
    # Two meanings if they fit comfortably, otherwise just the main one.
    result = meanings[0]
    if len(meanings) > 1 and len(result) + len(meanings[1]) < 140:
        result += "; " + meanings[1]
    return result


def import_from_web(db, how_many):
    """Walk further down the frequency list until we've added `how_many` words."""
    try:
        words = load_wordlist()
    except Offline:
        words = None
    if words is None:
        return 0, "couldn't download the word list, are you online?"

    position = int(get_state(db, "wordlist_position", 0))
    added = 0
    problem = None
    while added < how_many and position < len(words):
        word = normalize(words[position])
        rank = position + 1

        if looks_like_a_real_word(word) and not db.execute(
            "SELECT 1 FROM words WHERE word = ?", (word,)  # already known? don't ask Wiktionary
        ).fetchone():
            try:
                meaning = meaning_from_wiktionary(word)
            except Offline:
                # Stop here without moving past this word, so it gets
                # another chance next time instead of being skipped for good.
                problem = "lost the connection, will pick up from here next time"
                break
            if meaning and add_word(db, word, meaning, "web", rank):
                added += 1
            time.sleep(0.2)  # be polite to Wiktionary

        position += 1

    set_state(db, "wordlist_position", position)
    if position >= len(words):
        problem = "reached the end of the word list"
    return added, problem


def top_up_if_low(db):
    """Quietly fetch more words when the pile of unseen ones gets small."""
    if unseen_count(db) >= LOW_WATER_MARK:
        return
    last_try = float(get_state(db, "last_top_up_attempt", 0))
    if time.time() - last_try < RETRY_AFTER:
        return  # we tried recently and it didn't work out; don't hammer the web
    set_state(db, "last_top_up_attempt", time.time())
    import_from_web(db, TOP_UP_BATCH)


# ---------------------------------------------------------------------------
# The Mac side: the popup
# ---------------------------------------------------------------------------

def show_dialog(word, meaning):
    # "activate" pulls the popup in front of whatever window you have open,
    # otherwise it can quietly appear behind your browser.
    # Passing the text as arguments means quotes in a meaning can't break the script.
    script = [
        "on run argv",
        "activate",
        'display dialog (item 1 of argv) with title (item 2 of argv) '
        'buttons {"Kiitos!"} default button 1',
        "end run",
    ]
    cmd = ["osascript"]
    for line in script:
        cmd += ["-e", line]
    cmd += [f"{word}\n\n{meaning}", DIALOG_TITLE]
    subprocess.run(cmd, capture_output=True)


def show(db):
    pick = todays_word(db)
    if pick is None:
        show_dialog("No words yet", "Connect to the internet or add one with: wotd add")
    else:
        show_dialog(*pick)
    # While you're here and the popup is closed, restock if we're running low.
    top_up_if_low(db)


# ---------------------------------------------------------------------------
# Small helpers and the command line
# ---------------------------------------------------------------------------

def now():
    return datetime.datetime.now()


def main(argv):
    db = connect()
    command = argv[1] if len(argv) > 1 else "today"

    if command == "show":
        show(db)

    elif command == "today":
        pick = todays_word(db)
        print("No words yet. Try: wotd import 30" if pick is None else f"{pick[0]}  —  {pick[1]}")

    elif command == "add":
        if len(argv) < 4:
            print('Usage: wotd add <word> "<meaning>"')
            return 1
        word, meaning = argv[2], " ".join(argv[3:])
        if add_word(db, word, meaning, "manual"):
            print(f"Added {normalize(word)}. It'll come up before the web words.")
        else:
            print(f"{normalize(word)} is already in your list.")

    elif command == "import":
        how_many = int(argv[2]) if len(argv) > 2 else TOP_UP_BATCH
        print(f"Fetching {how_many} words, this takes a few seconds...")
        added, problem = import_from_web(db, how_many)
        print(f"Added {added} new words." + (f" ({problem})" if problem else ""))

    elif command == "stats":
        total = db.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        mine = db.execute("SELECT COUNT(*) FROM words WHERE source = 'manual'").fetchone()[0]
        print(f"{total} words ({mine} added by you), {unseen_count(db)} not seen yet.")

    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
