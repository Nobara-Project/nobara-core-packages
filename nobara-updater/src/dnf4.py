import logging
import queue
import threading
import time
from logging.handlers import QueueHandler
from typing import Any

import dnf  # type: ignore[import]
import gi  # type: ignore[import]

gi.require_version("Gtk", "3.0")

from gi.repository import Gtk  # type: ignore[import]


class AttributeDict(dict[str, Any]):
    def __init__(self, id: str, metalink: Any, mirrorlist: Any, baseurl: Any) -> None:
        super().__init__()
        self.id = id
        self.metalink = metalink
        self.mirrorlist = mirrorlist
        self.baseurl = baseurl

    def __getattr__(self, attr: str) -> Any:
        try:
            return self[attr]
        except KeyError as err:
            raise AttributeError(
                f"'AttributeDict' object has no attribute '{attr}'"
            ) from err

    def __setattr__(self, attr: str, value: Any) -> None:
        self[attr] = value

    def __delattr__(self, attr: str) -> None:
        try:
            del self[attr]
        except KeyError as err:
            raise AttributeError(
                f"'AttributeDict' object has no attribute '{attr}'"
            ) from err


def repoindex(retries: int = 3, delay: int = 5) -> list[AttributeDict]:
    attempt = 0
    logger = logging.getLogger()
    while attempt < retries:
        base = dnf.Base()
        try:
            base.read_all_repos()
            base.fill_sack(load_system_repo=True)

            def metadata_refresh(base: dnf.Base) -> None:
                for repo in base.repos.iter_enabled():
                    repo.metadata_expire = 0
                    repo.load()

            metadata_refresh(base)

            def get_enabled_repos(base: dnf.Base) -> list[AttributeDict]:
                enabled_repos = []
                for repo in base.repos.iter_enabled():
                    id = repo.id
                    metalink = None
                    mirrorlist = None
                    baseurl = None

                    if repo.metalink:
                        metalink = repo.metalink
                    if repo.mirrorlist:
                        mirrorlist = repo.mirrorlist
                    if repo.baseurl:
                        baseurl = []
                        for url in repo.baseurl:
                            baseurl.append(url)

                    repo_info = AttributeDict(
                        id=id,
                        metalink=metalink,
                        mirrorlist=mirrorlist,
                        baseurl=baseurl,
                    )
                    enabled_repos.append(repo_info)
                return enabled_repos

            return get_enabled_repos(base)
        except FileNotFoundError as e:
            if e.errno == 2:
                attempt += 1
                logger.info("Attempt %d failed with error: %s. Retrying in %d seconds...", attempt, e, delay)
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            logger.error("An unexpected error occurred: %s", e)
            raise
        finally:
            try:
                base.close()
            except Exception as e:
                logger.error("Failed to close base: %s", e)
    raise Exception("Failed to complete operation after %d attempts" % retries)


def updatechecker(retries: int = 3, delay: int = 5) -> list[str]:
    attempt = 0
    logger = logging.getLogger()
    while attempt < retries:
        base = dnf.Base()
        try:
            base.read_all_repos()

            def metadata_refresh(base: dnf.Base = base) -> None:
                for repo in base.repos.iter_enabled():
                    repo.metadata_expire = 0
                    repo.load()

            def get_repo_priority(repo_name: str, base: dnf.Base = base) -> int:
                repo = base.repos.get(repo_name)
                return repo.priority if repo else 99

            def get_package_repos(package_name: str, base: dnf.Base = base) -> list[str]:
                repos: set[str] = set()
                query = base.sack.query().available().filter(name=package_name)
                for pkg in query.run():
                    repos.add(pkg.reponame)
                return list(repos)

            base.fill_sack(load_system_repo=True)
            q = base.sack.query()
            updates = q.upgrades().run()

            metadata_refresh(base)

            latest_versions: dict[str, dnf.package.Package] = {}
            for pkg in updates:
                repos = get_package_repos(pkg.name, base)
                repo_priorities = [get_repo_priority(repo, base) for repo in repos]
                lowest_priority = min(repo_priorities) if repo_priorities else 99
                pkg_repo_priority = pkg.repo.priority

                if pkg_repo_priority == lowest_priority:
                    if pkg.name in latest_versions:
                        if pkg.evr > latest_versions[pkg.name].evr:
                            latest_versions[pkg.name] = pkg
                    else:
                        latest_versions[pkg.name] = pkg
            return [update.name for update in list(latest_versions.values())]
        except FileNotFoundError as e:
            if e.errno == 2:
                attempt += 1
                logger.info("Attempt %d failed with error: %s. Retrying in %d seconds...", attempt, e, delay)
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            logger.error("An unexpected error occurred: %s", e)
            raise
        finally:
            try:
                base.close()
            except Exception as e:
                logger.error("Failed to close base: %s", e)
    raise Exception("Failed to complete operation after %d attempts" % retries)


class PackageUpdater:
    def __init__(
        self,
        package_names: list[str],
        action: str,
        liststore: Gtk.ListStore,
        logger: logging.Logger | None = None,
    ):
        self.package_names = package_names
        self.liststore = liststore
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.queue_handler = QueueHandler(self.log_queue)
        self.logger = logger if logger is not None else logging.getLogger()
        self.logger.addHandler(self.queue_handler)
        self.logger.setLevel(logging.INFO)
        self.thread = threading.Thread(target=self.update_packages, args=(action,))
        self.thread.start()

    def update_packages(self, action: str, retries: int = 3, delay: int = 5) -> None:
        attempt = 0
        while attempt < retries:
            if self.package_names:
                try:
                    base = dnf.Base()
                    base.read_all_repos()
                    self.logger.info("Read all repos")
                    base.fill_sack()
                    action_log_string = "Upgrading packages:"

                    for package_name in self.package_names:
                        if action == "upgrade":
                            base.upgrade(package_name)
                        elif action == "install":
                            base.install(package_name)
                            action_log_string = "Installing packages:"
                        elif action == "remove":
                            base.remove(package_name)
                            action_log_string = "Removing packages:"

                    self.logger.info(
                        "%s\n%s", action_log_string, chr(10).join(self.package_names)
                    )

                    base.resolve()
                    self.logger.info("Resolved dependencies")

                    # Refresh metadata and download packages
                    base.download_packages(base.transaction.install_set)
                    self.logger.info("Downloaded packages")

                    # Perform the transaction
                    self.logger.info("Starting transaction")
                    base.do_transaction()
                    self.logger.info("Performed transaction")

                    self.logger.info("Successfully updated packages!")
                    return  # Exit the loop on success
                except FileNotFoundError as e:
                    if e.errno == 2:
                        attempt += 1
                        self.logger.info("Attempt %d failed with error: %s. Retrying in %d seconds...", attempt, e, delay)
                        time.sleep(delay)
                    else:
                        raise
                except Exception as e:
                    self.logger.error("An unexpected error occurred: %s", e)
                    raise
                finally:
                    try:
                        base.close()
                    except Exception as e:
                        self.logger.error("Failed to close base: %s", e)
        raise Exception("Failed to complete operation after %d attempts" % retries)
