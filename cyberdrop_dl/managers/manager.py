from __future__ import annotations

import copy
import json
from dataclasses import Field, field
from time import perf_counter
from typing import TYPE_CHECKING

from cyberdrop_dl import __version__
from cyberdrop_dl.managers.args_manager import ArgsManager
from cyberdrop_dl.managers.cache_manager import CacheManager
from cyberdrop_dl.managers.client_manager import ClientManager
from cyberdrop_dl.managers.config_manager import ConfigManager
from cyberdrop_dl.managers.db_manager import DBManager
from cyberdrop_dl.managers.download_manager import DownloadManager
from cyberdrop_dl.managers.hash_manager import HashManager
from cyberdrop_dl.managers.live_manager import LiveManager
from cyberdrop_dl.managers.log_manager import LogManager
from cyberdrop_dl.managers.path_manager import PathManager
from cyberdrop_dl.managers.progress_manager import ProgressManager
from cyberdrop_dl.managers.realdebrid_manager import RealDebridManager
from cyberdrop_dl.utils.args import config_definitions
from cyberdrop_dl.utils.data_enums_classes.supported_domains import SupportedDomains
from cyberdrop_dl.utils.logger import log
from cyberdrop_dl.utils.transfer.first_time_setup import TransitionManager

if TYPE_CHECKING:
    from asyncio import TaskGroup

    from cyberdrop_dl.scraper.scraper import ScrapeMapper


class Manager:
    def __init__(self) -> None:
        self.args_manager: ArgsManager = ArgsManager()
        self.cache_manager: CacheManager = CacheManager(self)
        self.path_manager: PathManager = field(init=False)
        self.config_manager: ConfigManager = field(init=False)
        self.hash_manager: HashManager = field(init=False)
        self.real_debrid_manager: RealDebridManager = field(init=False)

        self.log_manager: LogManager = field(init=False)
        self.db_manager: DBManager = field(init=False)
        self.client_manager: ClientManager = field(init=False)

        self.download_manager: DownloadManager = field(init=False)
        self.progress_manager: ProgressManager = field(init=False)
        self.live_manager: LiveManager = field(init=False)

        self.first_time_setup: TransitionManager = TransitionManager(self)

        self._loaded_args_config: bool = False
        self._made_portable: bool = False

        self.task_group: TaskGroup = field(init=False)
        self.task_list: list = []
        self.scrape_mapper: ScrapeMapper = field(init=False)

        self.vi_mode: bool = False
        self.start_time: float = perf_counter()
        self.downloaded_data: int = 0

    def startup(self) -> None:
        """Startup process for the manager."""
        self.args_startup()

        if not self.args_manager.appdata_dir:
            self.first_time_setup.startup()

        self.path_manager = PathManager(self)
        self.path_manager.pre_startup()

        self.cache_manager.startup(self.path_manager.cache_dir / "cache.yaml")
        self.config_manager = ConfigManager(self)
        self.config_manager.startup()
        self.vi_mode = (
            self.config_manager.global_settings_data["UI_Options"]["vi_mode"]
            if self.args_manager.vi_mode is None
            else self.args_manager.vi_mode
        )

        self.path_manager.startup()
        self.log_manager = LogManager(self)

        # Adjust settings for SimpCity update
        simp_settings_adjusted = self.cache_manager.get("simp_settings_adjusted")
        if not simp_settings_adjusted:
            for config in self.config_manager.get_configs():
                if config != self.config_manager.loaded_config:
                    self.config_manager.change_config(config)
                self.config_manager.settings_data["Runtime_Options"]["update_last_forum_post"] = True
                self.config_manager.write_updated_settings_config()
            global_settings = self.config_manager.global_settings_data
            if global_settings["Rate_Limiting_Options"]["download_attempts"] >= 10:
                global_settings["Rate_Limiting_Options"]["download_attempts"] = 5
            if global_settings["Rate_Limiting_Options"]["max_simultaneous_downloads_per_domain"] > 15:
                global_settings["Rate_Limiting_Options"]["max_simultaneous_downloads_per_domain"] = 5
            self.config_manager.write_updated_global_settings_config()
        self.cache_manager.save("simp_settings_adjusted", True)

    def args_startup(self) -> None:
        """Start the args manager."""
        if not self.args_manager.parsed_args:
            self.args_manager.startup()

    """~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~"""

    async def async_startup(self) -> None:
        """Async startup process for the manager."""
        self.args_consolidation()
        self.args_logging()

        if not isinstance(self.client_manager, ClientManager):
            self.client_manager = ClientManager(self)
        if not isinstance(self.download_manager, DownloadManager):
            self.download_manager = DownloadManager(self)
        if not isinstance(self.real_debrid_manager, RealDebridManager):
            self.real_debrid_manager = RealDebridManager(self)
        await self.async_db_hash_startup()

        # set files from args
        from cyberdrop_dl.utils import constants

        constants.MAX_NAME_LENGTHS["FILE"] = int(
            self.config_manager.global_settings_data["General"]["max_file_name_length"],
        )
        constants.MAX_NAME_LENGTHS["FOLDER"] = int(
            self.config_manager.global_settings_data["General"]["max_folder_name_length"],
        )

    async def async_db_hash_startup(self) -> None:
        # start up the db manager and hash manager only for scanning
        if not isinstance(self.db_manager, DBManager):
            self.db_manager = DBManager(self, self.path_manager.history_db)
            await self.db_manager.startup()
        if not isinstance(self.hash_manager, HashManager):
            self.hash_manager = HashManager(self)
            await self.hash_manager.startup()
        if not isinstance(self.live_manager, LiveManager):
            self.live_manager = LiveManager(self)
        self.progress_manager = ProgressManager(self)
        self.progress_manager.startup()

    def args_consolidation(self) -> None:
        """Consolidates runtime arguments with config values."""
        cli_settings_groups = ["Download_Options", "File_Size_Limits", "Ignore_Options", "Runtime_Options"]
        parsed_args = self.args_manager.parsed_args
        for arg in parsed_args:
            for cli_settings_group in cli_settings_groups:
                if arg in config_definitions.settings[cli_settings_group]:
                    if parsed_args[arg] == config_definitions.settings[cli_settings_group][arg]:
                        continue
                    if arg in self.args_manager.additive_args:
                        self.config_manager.settings_data[cli_settings_group][arg] += parsed_args[arg]
                    elif self.args_manager.parsed_args[arg] is not None:
                        self.config_manager.settings_data[cli_settings_group][arg] = parsed_args[arg]

    def args_logging(self) -> None:
        """Logs the runtime arguments."""
        forum_xf_cookies_provided = {}
        forum_credentials_provided = {}

        auth_data_forums = self.config_manager.authentication_data["Forums"]
        auth_data_others: dict[str, dict] = self.config_manager.authentication_data.copy()
        auth_data_others.pop("Forums", None)

        for forum in SupportedDomains.supported_forums_map.values():
            forum_xf_cookies_provided[forum] = bool(auth_data_forums[f"{forum}_xf_user_cookie"])
            forum_credentials_provided[forum] = bool(
                auth_data_forums[f"{forum}_username"] and auth_data_forums[f"{forum}_password"],
            )

        auth_provided = {
            "Forums Credentials": forum_credentials_provided,
            "Forums XF Cookies": forum_xf_cookies_provided,
        }

        for site, auth_entries in auth_data_others.items():
            auth_provided[site] = all(auth_entries.values())

        print_settings = copy.deepcopy(self.config_manager.settings_data)
        print_settings["Files"]["input_file"] = str(print_settings["Files"]["input_file"])
        print_settings["Files"]["download_folder"] = str(print_settings["Files"]["download_folder"])
        print_settings["Logs"]["log_folder"] = str(print_settings["Logs"]["log_folder"])
        print_settings["Logs"]["webhook_url"] = bool(print_settings["Logs"]["webhook_url"])
        print_settings["Sorting"]["sort_folder"] = str(print_settings["Sorting"]["sort_folder"])
        print_settings["Sorting"]["scan_folder"] = str(print_settings["Sorting"]["scan_folder"]) or ""
        print_settings["Dupe_Cleanup_Options"]["hashing"] = print_settings["Dupe_Cleanup_Options"]["hashing"].name

        log(f"Starting Cyberdrop-DL Process - Config: {self.config_manager.loaded_config}", 10)
        log(f"Running version {__version__}", 10)
        log(f"Using Config: {self.config_manager.loaded_config}", 10)
        log(f"Using Config File: {self.config_manager.settings.resolve()}", 10)
        log(f"Using Input File: {self.path_manager.input_file.resolve()}", 10)
        log(f"Using Download Folder: {self.path_manager.download_dir.resolve()}", 10)
        log(f"Using History File: {self.path_manager.history_db.resolve()}", 10)

        log(f"Using Authentication: \n{json.dumps(auth_provided, indent=4, sort_keys=True)}", 10)
        log(f"Using Settings: \n{json.dumps(print_settings, indent=4, sort_keys=True)}", 10)
        log(
            f"Using Global Settings: \n{json.dumps(self.config_manager.global_settings_data, indent=4, sort_keys=True)}",
            10,
        )

    async def close(self) -> None:
        """Closes the manager."""
        await self.db_manager.close()
        if not isinstance(self.client_manager, Field):
            await self.client_manager.close()
        self.db_manager: DBManager = field(init=False)
        self.cache_manager: CacheManager = field(init=False)
        self.hash_manager: HashManager = field(init=False)
