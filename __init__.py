import configparser
import platform
import shutil
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import List, Tuple, Callable

from albert import *

md_iid = "5.0"
md_version = "1.0.0"
md_name = "Firefox"
md_description = "Access Firefox bookmarks and history"
md_license = "MIT"
md_url = "https://github.com/tomsquest/albert_plugin_firefox_bookmarks"
md_readme_url = "https://github.com/albertlauncher/albert-plugin-python-firefox/blob/main/README.md"
md_authors = ["@tomsquest"]
md_maintainers = ["@tomsquest"]
md_credits = ["@stevenxxiu", "@sagebind"]


def get_available_profiles(firefox_root: Path) -> List[str]:
    """Get list of available Firefox profiles from profiles.ini"""
    profiles = []

    if not firefox_root.exists():
        return profiles

    try:
        config = configparser.ConfigParser()
        config.read(firefox_root / "profiles.ini")

        for section in config.sections():
            if section.startswith("Profile") and "Path" in config[section]:
                profile_path = firefox_root / config[section]["Path"]
                if (profile_path / "places.sqlite").exists() and (
                    profile_path / "favicons.sqlite"
                ).exists():
                    profiles.append(config[section]["Path"])

    except Exception as e:
        warning(f"Failed to read Firefox profiles: {str(e)}")

    return profiles


@contextmanager
def get_connection(db_path: Path):
    """Create a connection to the places database with read-only access.

    Copies the database files to a temporary directory to avoid lock issues
    when Firefox is running.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Places database not found at {db_path}")

    # Create a temporary directory for the database copy
    temp_dir = tempfile.mkdtemp(prefix="albert_plugin_firefox_db_")
    temp_dir_path = Path(temp_dir)

    try:
        # Copy the main database file and its auxiliary files (WAL, SHM)
        for suffix in ["", "-wal", "-shm"]:
            src_file = db_path.parent / f"{db_path.name}{suffix}"
            if src_file.exists():
                shutil.copy2(src_file, temp_dir_path / src_file.name)

        # Connect to the copied database
        temp_db_path = temp_dir_path / db_path.name
        conn = sqlite3.connect(temp_db_path)

        try:
            # Integrate possible changes in wal files
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            yield conn
        finally:
            conn.close()
    finally:
        # Clean up the temporary directory
        shutil.rmtree(temp_dir, ignore_errors=True)


def get_bookmarks(places_db: Path) -> List[Tuple[str, str, str, str]]:
    """Get all bookmarks from the places database"""
    try:
        with get_connection(places_db) as conn:
            cursor = conn.cursor()

            # Query bookmarks
            cursor.execute("""
                SELECT bookmark.guid, bookmark.title, place.url, place.url_hash
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


def get_history(places_db: Path) -> List[Tuple[str, str, str]]:
    """Get all history items from the places database"""
    try:
        with get_connection(places_db) as conn:
            cursor = conn.cursor()

            # Query history excluding bookmarks
            cursor.execute("""
                SELECT place.guid, place.title, place.url
                FROM moz_places place
                  LEFT JOIN moz_bookmarks bookmark ON place.id = bookmark.fk
                WHERE place.hidden = 0
                  AND place.url IS NOT NULL
                  AND bookmark.id IS NULL
            """)

            return cursor.fetchall()

    except sqlite3.Error as e:
        critical(f"Failed to read Firefox history: {str(e)}")
        return []


def get_recent_history(places_db: Path, search: str = "", limit: int = 50) -> List[Tuple[str, str, str]]:
    """Get history items ordered by most recently visited, optionally filtered by search term.

    :param places_db: Path to the places.sqlite database
    :param search: Optional search string to filter by title or URL
    :param limit: Maximum number of results to return
    """
    try:
        with get_connection(places_db) as conn:
            cursor = conn.cursor()

            search_clause = "1=1"
            params: dict = {"limit": limit}
            if search:
                search_clause = "(place.title LIKE :search OR place.url LIKE :search)"
                params["search"] = f"%{search}%"

            query = f"""
                SELECT place.guid, place.title, place.url
                FROM moz_places place
                  LEFT JOIN moz_bookmarks bookmark ON place.id = bookmark.fk
                WHERE place.hidden = 0
                  AND place.url IS NOT NULL
                  AND place.last_visit_date IS NOT NULL
                  AND bookmark.id IS NULL
                  AND {search_clause}
                ORDER BY place.last_visit_date DESC
                LIMIT :limit
            """

            cursor.execute(query, params)
            return cursor.fetchall()

    except sqlite3.Error as e:
        critical(f"Failed to read Firefox recent history: {str(e)}")
        return []


def get_favicons_data(favicons_db: Path) -> dict[str, bytes]:
    """Get all favicon data from the favicons database"""
    try:
        with get_connection(favicons_db) as conn:
            cursor = conn.cursor()

            # Query favicons
            cursor.execute("""
                SELECT moz_pages_w_icons.page_url_hash, moz_icons.data
                FROM moz_icons
                  INNER JOIN moz_icons_to_pages ON moz_icons.id = moz_icons_to_pages.icon_id
                  INNER JOIN moz_pages_w_icons ON moz_icons_to_pages.page_id = moz_pages_w_icons.id
            """)

            return {row[0]: row[1] for row in cursor.fetchall()}

    except sqlite3.Error as e:
        warning(f"Failed to read favicon data: {str(e)}")
        return {}


class FirefoxQueryHandler(IndexQueryHandler):
    """Handles fuzzy search over Firefox bookmarks and optionally history."""

    def __init__(self,
                 profile_path: Path,
                 data_location: Path,
                 icon_factory: Callable[[], Icon],
                 index_history: bool = False,
    ):
        """
        :param profile_path: Path to the profile
        :param data_location: Path to the recommended plugin data location to store icons
        :param icon_factory: Callable with no arguments that returns an Icon
                             to be used for Firefox results
        :param index_history: If true, history is also indexed
        """
        IndexQueryHandler.__init__(self)
        self.thread = None

        self.profile_path = profile_path
        self.icon_factory = icon_factory
        self.index_history = index_history
        self.plugin_data_location = data_location

    def id(self) -> str:
        """
        Returns the extension identifier.
        """
        return md_name

    def name(self) -> str:
        """
        Returns the pretty, human readable extension name.
        """
        return md_name

    def description(self) -> str:
        """
        Returns the brief extension description.
        """
        return md_description

    def __del__(self):
        if self.thread and self.thread.is_alive():
            self.thread.join()

    def defaultTrigger(self):
        return "f "

    def updateIndexItems(self):
        if self.thread and self.thread.is_alive():
            self.thread.join()
        self.thread = threading.Thread(target=self._update_index_items_task)
        self.thread.start()

    def _update_index_items_task(self):
        places_db = self.profile_path/ "places.sqlite"
        favicons_db = self.profile_path / "favicons.sqlite"

        bookmarks = get_bookmarks(places_db)
        info(f"Found {len(bookmarks)} bookmarks")

        favicons_location = self.plugin_data_location / "favicons"
        favicons_location.mkdir(exist_ok=True, parents=True)

        for f in favicons_location.glob("*"):
            f.unlink()

        favicons = get_favicons_data(favicons_db)

        index_items = []
        seen_urls = set()

        for guid, title, url, url_hash in bookmarks:
            if url in seen_urls:
                continue
            seen_urls.add(url)

            favicon_data = favicons.get(url_hash)
            if favicon_data:
                favicon_path = favicons_location / f"favicon_{guid}.png"
                with open(favicon_path, "wb") as f:
                    f.write(favicon_data)
                icon_factory = lambda p=favicon_path: Icon.composed(
                    self.icon_factory(), Icon.iconified(Icon.image(p)), 1.0, .7)
            else:
                icon_factory = lambda: Icon.composed(
                    self.icon_factory(), Icon.grapheme("🌐"), 1.0, .7)

            item = StandardItem(
                id=guid,
                text=title if title else url,
                subtext=url,
                icon_factory=icon_factory,
                actions=[
                    Action("open", "Open in Firefox", lambda u=url: openUrl(u)),
                    Action("copy", "Copy URL", lambda u=url: setClipboardText(u)),
                ],
            )
            index_items.append(IndexItem(item=item, string=f"{title} {url}".lower()))

        if self.index_history:
            history = get_history(places_db)
            info(f"Found {len(history)} history items")
            for guid, title, url in history:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                item = StandardItem(
                    id=guid,
                    text=title if title else url,
                    subtext=url,
                    icon_factory=lambda: Icon.composed(
                        self.icon_factory(), Icon.grapheme("🕘"), 1.0),
                    actions=[
                        Action("open", "Open in Firefox", lambda u=url: openUrl(u)),
                        Action("copy", "Copy URL", lambda u=url: setClipboardText(u)),
                    ],
                )
                index_items.append(IndexItem(item=item, string=f"{title} {url}".lower()))

        self.setIndexItems(index_items)


class FirefoxHistoryHandler(GeneratorQueryHandler):
    """Yields Firefox history ordered by most recently visited."""

    def __init__(self, profile_path: Path, icon_factory):
        """
        :param profile_path: Path to the Firefox profile directory
        :param icon_factory: Callable returning the Firefox icon
        """
        GeneratorQueryHandler.__init__(self)
        self.profile_path = profile_path
        self.icon_factory = icon_factory

    def id(self) -> str:
        return md_name + "_history"

    def name(self) -> str:
        return md_name + " History"

    def description(self) -> str:
        return "Browse Firefox history ordered by most recently visited"

    def defaultTrigger(self):
        return "fh "

    def items(self, context: QueryContext):
        places_db = self.profile_path / "places.sqlite"
        history = get_recent_history(places_db, search=context.query.strip())

        yield [
            StandardItem(
                id=guid,
                text=title if title else url,
                subtext=url,
                icon_factory=lambda: Icon.composed(self.icon_factory(), Icon.grapheme("🕘"), 1.0),
                actions=[
                    Action("open", "Open in Firefox", lambda u=url: openUrl(u)),
                    Action("copy", "Copy URL", lambda u=url: setClipboardText(u)),
                ],
            )
            for guid, title, url in history
        ]


class Plugin(PluginInstance):
    """Owns shared Firefox state and configuration."""

    def __init__(self):
        PluginInstance.__init__(self)

        # Get the Firefox root directory
        match platform.system():
            case "Darwin":
                self.firefox_data_dir = Path.home() / "Library" / "Application Support" / "Firefox"
                self.firefox_icon_factory = lambda: Icon.fileType("/Applications/Firefox.app")
            case "Linux":
                self.firefox_data_dir = Path.home() / ".mozilla" / "firefox"
                self.firefox_icon_factory = lambda: Icon.theme("firefox")
            case _:
                raise NotImplementedError(f"Unsupported platform: {platform.system()}")

        # Get available profiles
        self.profiles = get_available_profiles(self.firefox_data_dir)
        if not self.profiles:
            raise RuntimeError("No Firefox profiles found")

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

        self.handler = FirefoxQueryHandler(
            profile_path=self.firefox_data_dir / self.current_profile_path,
            data_location=Path(self.dataLocation()),
            icon_factory=self.firefox_icon_factory,
            index_history=self._index_history,
        )
        self.handler.updateIndexItems()

        self.history_handler = FirefoxHistoryHandler(
            profile_path=self.firefox_data_dir / self.current_profile_path,
            icon_factory=self.firefox_icon_factory,
        )

    def extensions(self):
        return [self.handler, self.history_handler]

    @property
    def current_profile_path(self):
        return self._current_profile_path

    @current_profile_path.setter
    def current_profile_path(self, value):
        self._current_profile_path = value
        self.writeConfig("current_profile_path", value)

        # Update handlers to point to the newly selected profile before reindexing
        new_profile_path = self.firefox_data_dir / value
        self.handler.profile_path = new_profile_path
        self.history_handler.profile_path = new_profile_path
        self.handler.updateIndexItems()

    @property
    def index_history(self):
        return self._index_history

    @index_history.setter
    def index_history(self, value):
        self._index_history = value
        self.writeConfig("index_history", value)
        # Ensure the query handler uses the updated history indexing setting
        self.handler.index_history = value
        self.handler.updateIndexItems()

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