import atexit
import logging
import sqlite3
from pathlib import Path

from cyberdrop_dl.base_functions.base_functions import get_db_path
from cyberdrop_dl.base_functions.data_classes import AlbumItem, CascadeItem, DomainItem


class SQLHelper:
    """This class is responsible for handling SQL operations"""
    def __init__(self, ignore_history, ignore_cache, download_history):
        self.ignore_history = ignore_history
        self.ignore_cache = ignore_cache
        self.download_history = download_history
        self.conn = None
        self.curs = None

        self.old_history = False
        # Close the sql connection when the program exits
        atexit.register(self.exit_handler)

    async def sql_initialize(self):
        """Initializes the SQL connection, and makes sure necessary tables exist"""
        self.conn = sqlite3.connect(self.download_history)
        self.curs = self.conn.cursor()

        await self.check_old_history()

        await self.pre_allocate()
        await self.create_media_history()
        await self.create_coomeno_history()

    async def check_old_history(self):
        """Checks whether V3 history exists"""
        self.curs.execute("""SELECT name FROM sqlite_schema WHERE type='table' AND name='downloads'""")
        sql_file_check = self.curs.fetchone()
        if sql_file_check:
            self.old_history = True

    async def create_media_history(self):
        """We create the download history tables here"""
        create_table_query = """CREATE TABLE IF NOT EXISTS media (
                                                    domain TEXT,
                                                    url_path TEXT,
                                                    album_path TEXT,
                                                    referer TEXT,
                                                    download_path TEXT,
                                                    download_filename TEXT,
                                                    original_filename TEXT,
                                                    completed INTEGER NOT NULL,
                                                    PRIMARY KEY (url_path, original_filename)
                                                );"""
        create_temp_download_name_query = """CREATE TABLE IF NOT EXISTS downloads_temp (
                                                                downloaded_filename TEXT
                                                            );"""
        temp_truncate_query = """DELETE FROM downloads_temp;"""

        self.curs.execute(create_table_query)
        self.conn.commit()
        self.curs.execute(create_temp_download_name_query)
        self.conn.commit()
        self.curs.execute(temp_truncate_query)
        self.conn.commit()

    async def create_coomeno_history(self):
        """Creates the cache table for coomeno"""
        create_table_query = """CREATE TABLE IF NOT EXISTS coomeno (
                                                            url_path TEXT,
                                                            post_data BLOB,
                                                            PRIMARY KEY (url_path)
                                                        );"""
        self.curs.execute(create_table_query)
        self.conn.commit()

    async def pre_allocate(self):
        """We pre-allocate 50MB of space to the SQL file just in case the user runs out of disk space"""
        pre_alloc = "CREATE TABLE IF NOT EXISTS t(x);"
        pre_alloc2 = "INSERT INTO t VALUES(zeroblob(50*1024*1024));"  # 50 mb
        drop_pre = "DROP TABLE t;"
        check_prealloc = "PRAGMA freelist_count;"

        self.curs.execute(check_prealloc)
        free = self.curs.fetchone()[0]
        if free <= 1024:
            self.curs.execute(pre_alloc)
            self.conn.commit()
            self.curs.execute(pre_alloc2)
            self.conn.commit()
            self.curs.execute(drop_pre)
            self.conn.commit()

    """Temp Table Operations"""

    async def get_temp_names(self):
        """Gets the list of temp filenames"""
        self.curs.execute("SELECT downloaded_filename FROM downloads_temp;")
        filenames = self.curs.fetchall()
        filenames = list(sum(filenames, ()))
        return filenames

    async def sql_insert_temp(self, downloaded_filename):
        """Inserts a temp filename into the downloads_temp table"""
        self.curs.execute("""INSERT OR IGNORE INTO downloads_temp VALUES (?)""", (downloaded_filename,))
        self.conn.commit()

    """Coomeno Table Operations"""

    async def insert_blob(self, blob: str, url_path: str):
        """Inserts the post content into coomeno"""
        self.curs.execute("""INSERT OR IGNORE INTO coomeno VALUES (?, ?)""", (url_path, blob,))
        self.conn.commit()

    async def get_blob(self, url_path: str):
        """returns the post content for a given coomeno post url"""
        if self.ignore_cache:
            return None
        self.curs.execute("""SELECT post_data FROM coomeno WHERE url_path = ?""", (url_path,))
        sql_file_check = self.curs.fetchone()
        if sql_file_check:
            return sql_file_check[0]
        return None

    """Download Table Operations"""

    async def insert_media(self, domain: str, url_path: str, album_path: str, referer: str, download_path: str,
                           download_filename: str, original_filename: str, completed: int):
        """Inserts a media entry into the media table"""
        self.curs.execute("""INSERT OR IGNORE INTO media VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                          (domain, url_path, album_path, referer, download_path, download_filename, original_filename, completed,))
        self.conn.commit()

    async def insert_album(self, domain: str, album_path: str, album: AlbumItem):
        """Inserts an albums media into the media table"""
        if album.media:
            for media in album.media:
                url_path = await get_db_path(media.url, domain)
                self.curs.execute("""INSERT OR IGNORE INTO media VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                  (domain, url_path, album_path, str(media.referer), "", "", media.filename, 0,))
        self.conn.commit()

    async def insert_domain(self, domain_name: str, album_path: str, domain: DomainItem):
        """Inserts a domains media into the media table"""
        if domain.albums:
            for title, album in domain.albums.items():
                for media in album.media:
                    url_path = await get_db_path(media.url, domain_name)
                    self.curs.execute("""INSERT OR IGNORE INTO media VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                      (domain_name, url_path, album_path, str(media.referer), "", "",
                                       media.filename, 0,))
        self.conn.commit()

    async def insert_cascade(self, cascade: CascadeItem):
        """Inserts a cascades media into the media table"""
        if not await cascade.is_empty():
            for domain, domain_obj in cascade.domains.items():
                for title, album_obj in domain_obj.albums.items():
                    for media in album_obj.media:
                        url_path = await get_db_path(media.url, domain)
                        self.curs.execute("""INSERT OR IGNORE INTO media VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                          (domain, url_path, media.referer.path, str(media.referer), "",
                                           "", media.filename, 0,))
        self.conn.commit()

    async def get_downloaded_filename(self, url_path, filename):
        """Gets downloaded filename given the url path and original filename"""
        self.curs.execute("""SELECT download_filename FROM media WHERE url_path = ? and original_filename = ?""",
                          (url_path, filename,))
        sql_file_check = self.curs.fetchone()
        if sql_file_check:
            return sql_file_check[0]
        return None

    async def sql_check_old_existing(self, url_path):
        """Checks the V3 history table for completed if it exists"""
        if not self.old_history:
            return False
        if self.ignore_history:
            return False
        self.curs.execute("""SELECT completed FROM downloads WHERE path = ?""", (url_path,))
        sql_file_check = self.curs.fetchone()
        return sql_file_check and sql_file_check[0] == 1

    async def check_complete_singular(self, domain, url_path):
        """Checks whether an individual file has completed given its domain and url path"""
        if self.ignore_history:
            return False
        self.curs.execute("""SELECT completed FROM media WHERE domain = ? and url_path = ?""", (domain, url_path,))
        sql_file_check = self.curs.fetchone()
        if not sql_file_check:
            return False
        elif sql_file_check[0] == 0:
            return False
        else:
            return True

    """Downloader Operations"""

    async def check_filename(self, filename):
        """Checks whether an individual exists in the DB given its filename"""
        self.curs.execute("""SELECT EXISTS(SELECT 1 FROM media WHERE download_filename = ?)""", (filename, ))
        sql_check = self.curs.fetchone()[0]
        return sql_check == 1

    async def update_pre_download(self, path: Path, filename: str, url_path: str, original_filename: str):
        """Update the media entry pre-download"""
        self.curs.execute("""UPDATE media SET download_path = ?, download_filename = ? WHERE url_path = ? 
        AND original_filename = ?""", (str(path), filename, url_path, original_filename,))
        self.conn.commit()

    async def mark_complete(self, url_path: str, original_filename: str):
        """Update the media entry post-download"""
        self.curs.execute("""UPDATE media SET completed = 1 WHERE url_path = ? AND original_filename = ?""", (url_path, original_filename,))
        self.conn.commit()

    def exit_handler(self):
        """Exit handler on unexpected exits"""
        try:
            self.conn.commit()
            self.conn.close()
        except Exception as e:
            logging.debug(f"Failed to close sqlite database connection: {str(e)}")
        else:
            logging.debug("Successfully closed sqlite database connection")
