import configparser
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import List

from albert import *

md_iid = "2.3"
md_version = "1.0"
md_name = "Firefox Bookmarks"
md_description = "Access Firefox bookmarks"
md_license = "MIT"
md_url = "https://github.com/tomsquest/albert_plugin_firefox_bookmarks"
md_authors = "@tomsquest"
md_lib_dependencies = ["sqlite3"]
md_credits = ["@stevenxxiu", "@sagebind"]
default_trigger = "f "


def get_firefox_root() -> Path:
    """Get the Firefox root directory"""
    return Path.home() / ".mozilla" / "firefox"


def get_available_profiles() -> List[str]:
    """Get list of available Firefox profiles from profiles.ini"""
    profiles = []
    firefox_root = get_firefox_root()

    if not firefox_root.exists():
        return profiles

    try:
        config = configparser.ConfigParser()
        config.read(firefox_root / "profiles.ini")

        for section in config.sections():
            if section.startswith("Profile") and "Path" in config[section]:
                profiles.append(config[section]["Path"])

    except Exception as e:
        warning(f"Failed to read Firefox profiles: {str(e)}")

    return profiles


@contextmanager
def get_connection(db_path: Path):
    """Create a connection to the places database with read-only access"""
    if not db_path.exists():
        raise FileNotFoundError(f"Places database not found at {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
    try:
        yield conn
    finally:
        conn.close()


def get_bookmarks(places_db: Path):
    """Get all bookmarks from the places database"""
    try:
        with get_connection(places_db) as conn:
            cursor = conn.cursor()

            # Query bookmarks
            cursor.execute("""
                SELECT bookmark.guid, bookmark.title, place.url
                FROM moz_bookmarks bookmark
                  JOIN moz_places place ON place.id = bookmark.fk
                WHERE bookmark.type = 1 -- 1 = bookmark
                  AND place.hidden = 0
                  AND place.url IS NOT NULL
            """)

            return cursor.fetchall()

    except sqlite3.Error as e:
        critical(f"Failed to read Firefox bookmarks: {str(e)}")
        return []


def get_history(places_db: Path):
    """Get all history items from the places database"""
    try:
        with get_connection(places_db) as conn:
            cursor = conn.cursor()

            # Query history
            cursor.execute("""
                SELECT h.id, h.title, h.url
                FROM moz_places h
                WHERE h.hidden = 0
                  AND h.url IS NOT NULL
            """)

            return cursor.fetchall()

    except sqlite3.Error as e:
        critical(f"Failed to read Firefox history: {str(e)}")
        return []


class Plugin(PluginInstance, IndexQueryHandler):
    def __init__(self):
        PluginInstance.__init__(self)
        IndexQueryHandler.__init__(
            self, self.id, self.name, self.description, defaultTrigger=default_trigger
        )
        self.thread = None

        # Get available profiles
        self.profiles = get_available_profiles()
        if not self.profiles:
            critical("No Firefox profiles found")
            return

        # Initialize profile selection
        self._current_profile_path = self.readConfig("current_profile_path", str)
        if self._current_profile_path not in self.profiles:
            # Use first profile as default if current profile is not valid
            self._current_profile_path = self.profiles[0]
            self.writeConfig("current_profile_path", self._current_profile_path)

        # Initialize history indexing preference
        self._index_history = self.readConfig("index_history", bool)
        if self._index_history is None:
            self._index_history = False
            self.writeConfig("index_history", self._index_history)

    def __del__(self):
        if self.thread and self.thread.is_alive():
            self.thread.join()

    @property
    def current_profile_path(self):
        return self._current_profile_path

    @current_profile_path.setter
    def current_profile_path(self, value):
        self._current_profile_path = value
        self.writeConfig("current_profile_path", value)
        self.updateIndexItems()

    @property
    def index_history(self):
        return self._index_history

    @index_history.setter
    def index_history(self, value):
        self._index_history = value
        self.writeConfig("index_history", value)
        self.updateIndexItems()

    def configWidget(self):
        return [
            {
                "type": "combobox",
                "property": "current_profile_path",
                "label": "Firefox Profile",
                "items": self.profiles,
                "widget_properties": {
                    "toolTip": "Select Firefox profile to search bookmarks from"
                },
            },
            {
                "type": "checkbox",
                "property": "index_history",
                "label": "Index Firefox History",
                "widget_properties": {
                    "toolTip": "Enable or disable indexing of Firefox history"
                },
            },
        ]

    def updateIndexItems(self):
        if self.thread and self.thread.is_alive():
            self.thread.join()
        self.thread = threading.Thread(target=self.update_index_items_task)
        self.thread.start()

    def update_index_items_task(self):
        places_db = get_firefox_root() / self.current_profile_path / "places.sqlite"
        bookmarks = get_bookmarks(places_db)
        info(f"Found {len(bookmarks)} bookmarks")

        index_items = []
        seen_urls = set()

        for guid, title, url in bookmarks:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            item = StandardItem(
                id=guid,
                text=title if title else url,
                subtext=url,
                iconUrls=[f"file:{Path(__file__).parent}/firefox_bookmark.svg", "xdg:firefox"],
                actions=[
                    Action("open", "Open in Firefox", lambda u=url: openUrl(u)),
                    Action("copy", "Copy URL", lambda u=url: setClipboardText(u)),
                ],
            )

            # Create searchable string for the bookmark
            index_items.append(IndexItem(item=item, string=f"{title} {url}".lower()))

        if self._index_history:
            history = get_history(places_db)
            info(f"Found {len(history)} history items")
            for id, title, url in history:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                item = StandardItem(
                    id=str(id),
                    text=title if title else url,
                    subtext=url,
                    iconUrls=[f"file:{Path(__file__).parent}/firefox_history.svg", "xdg:firefox"],
                    actions=[
                        Action("open", "Open in Firefox", lambda u=url: openUrl(u)),
                        Action("copy", "Copy URL", lambda u=url: setClipboardText(u)),
                    ],
                )

                # Create searchable string for the history item
                index_items.append(
                    IndexItem(item=item, string=f"{title} {url}".lower())
                )

        self.setIndexItems(index_items)
